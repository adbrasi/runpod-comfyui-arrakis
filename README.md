# Runpod ComfyUI Universal

Base de producao para rodar ComfyUI no Runpod Serverless com uma API unica para workflows arbitrarios, modelos no volume e escala simples de manter.

## O que esta pronto

- `handler.py` universal para `workflow + overrides + assets + outputs genericos`
- custom nodes instalados na imagem
- modelos fora da imagem por padrao, em `Network Volume`
- provisionamento idempotente de template, endpoint, secrets, auth e volumes
- suporte real a endpoint nativo multi-regiao com um volume por datacenter
- scripts de smoke test, auditoria de modelos e teste de carga

## Estrutura

```text
Dockerfile
handler.py
config/
  custom_nodes.json
  custom_nodes.ltx-lipsync.example.json
  deploy.example.json
  deploy.heavy.example.json
  deploy.multi-region.example.json
  models.json
  models.chenkin.example.json
  models.flux-dev.example.json
  models.ltx-lipsync.example.json
  models.sdxl.example.json
scripts/
  audit_workflow_models.py
  download_models.py
  install_custom_nodes.py
  load_test_endpoint.py
  provision_runpod.py
  smoke_test_endpoint.py
  sync_models_to_volume.py
tests/
```

## Arquitetura recomendada

O desenho final recomendado para este projeto ficou assim:

- 1 endpoint serverless principal para imagem
- 2 datacenters no mesmo endpoint quando voce quiser resiliencia global
- 1 network volume por datacenter
- custom nodes dentro da imagem
- modelos dentro dos volumes
- object storage apenas para outputs grandes

Isso evita rebuild de imagem por troca de modelo, reduz manutencao e nao obriga separar endpoint por regiao no backend.

## Como escalar sem complicar

No Runpod, `workersMax` e o teto total de GPUs simultaneas daquele endpoint.

Exemplos:

- `workersMax=5`: ate 5 GPUs simultaneas
- `workersMax=10`: ate 10 GPUs simultaneas
- `workersMax=20`: ate 20 GPUs simultaneas, desde que sua conta tenha quota para isso

Com um endpoint multi-regiao nativo, o Runpod pode distribuir workers entre os datacenters permitidos pelo endpoint e pelos volumes anexados. Para isso funcionar com modelos no volume:

- cada datacenter precisa do seu proprio volume
- os mesmos modelos precisam existir em cada volume
- `data_center_ids` do endpoint precisam bater com os datacenters dos volumes

Se voce quer escolher manualmente uma regiao especifica por request, o caminho e separar endpoints. Se voce quer simplicidade operacional, o endpoint global multi-regiao e melhor.

## Contrato da API

O endpoint segue o padrao do Runpod:

- `POST /run`
- `POST /runsync`
- `GET /status/{job_id}`
- `GET /health`

Payload:

```json
{
  "input": {
    "workflow": {},
    "overrides": [
      {
        "node": "6",
        "field": "text",
        "value": "cinematic portrait of an astronaut"
      }
    ],
    "assets": [
      {
        "name": "reference.png",
        "url": "https://example.com/reference.png"
      }
    ],
    "output_mode": "auto"
  }
}
```

Aliases aceitos:

- `overrides` ou `inputs`
- `assets`, `files`, `media` ou `images`
- em `images`, `image` funciona como alias de `data`

`output_mode`:

- `auto`: usa S3/R2 se configurado, senao retorna inline
- `inline` ou `base64`: retorna base64
- `object_store` ou `s3`: retorna URL do storage

## Handler e protecoes operacionais

O `handler.py` foi reforcado para uso diario:

- valida URLs remotas e bloqueia hosts locais e IPs privados
- baixa assets por URL ou base64
- aplica overrides sem remontar workflow no backend
- coleta `images`, `videos`, `gifs`, `audio`, escalares e JSON
- limpa arquivos de input temporarios por job
- limpa outputs gerados no `/comfyui/output` para nao lotar disco do worker com o tempo
- faz retry curto ate o `history` do ComfyUI realmente aparecer

Esse cleanup de output e importante em serverless porque o mesmo worker pode atender varios jobs antes de escalar para zero.

## Modelos

### Caminho padrao: Network Volume

Estrutura esperada:

```text
/runpod-volume/models/checkpoints/
/runpod-volume/models/loras/
/runpod-volume/models/vae/
/runpod-volume/models/controlnet/
...
```

Para workloads universais com varios checkpoints, LoRAs e workflows, esse e o caminho mais simples.

### Bake na imagem

Tambem suportado para casos muito fixos:

```bash
docker build --platform linux/amd64 --build-arg BAKE_MODELS=true -t runpod-comfyui-universal .
```

Use isso so quando o conjunto de modelos for pequeno e quase imutavel.

## Semear um ou varios volumes

Crie as credenciais S3 API do Runpod no painel e depois rode:

```bash
export RUNPOD_VOLUME_ACCESS_KEY_ID=...
export RUNPOD_VOLUME_SECRET_ACCESS_KEY=...

python scripts/sync_models_to_volume.py \
  --config config/models.chenkin.example.json \
  --target <VOLUME_ID_EU>:EU-RO-1 \
  --target <VOLUME_ID_US>:US-NC-1
```

O mesmo script continua aceitando o modo legado de um unico volume:

```bash
python scripts/sync_models_to_volume.py \
  --config config/models.sdxl.example.json \
  --volume-id <NETWORK_VOLUME_ID> \
  --data-center-id EU-RO-1
```

## Custom Nodes

Edite `config/custom_nodes.json`.

Exemplo por commit:

```json
{
  "nodes": [
    {
      "name": "ComfyUI-WanVideoWrapper",
      "type": "git",
      "url": "https://github.com/kijai/ComfyUI-WanVideoWrapper",
      "commit": "df8f3e49daaad117cf3090cc916c83f3d001494c"
    }
  ]
}
```

## Provisionamento

Perfis incluidos:

- `config/deploy.example.json`: canario single-region
- `config/deploy.multi-region.example.json`: endpoint global simples
- `config/deploy.heavy.example.json`: workload pesado separado

Passos:

```bash
export RUNPOD_API_KEY=...
export GHCR_USERNAME=seu-usuario
export GHCR_TOKEN=seu-token
export R2_ACCESS_KEY_ID=...
export R2_SECRET_ACCESS_KEY=...

python scripts/provision_runpod.py --config config/deploy.multi-region.example.json
```

O script:

- cria ou atualiza volumes
- cria ou atualiza template
- cria ou atualiza endpoint
- usa GraphQL quando precisa salvar endpoint nativo multi-regiao com varios volumes
- devolve IDs, URL do endpoint e endpoints S3 dos volumes

## Configuracao de producao sugerida

Ponto de partida simples:

- `flashboot=true`
- `workers_min=1`
- `workers_max=10`
- `idle_timeout=5`
- `scaler_type=QUEUE_DELAY`
- `scaler_value=2`

Se custo for prioridade maior que latencia:

- reduza `workers_min` para `0`

Se fila virar rotina:

- aumente `workers_max`
- ou peca aumento de quota para o Runpod

Se video e Flux pesado comecarem a afetar a fila de imagem:

- crie um segundo endpoint com `deploy.heavy`

## Testes

### Auditoria de modelos

```bash
python scripts/audit_workflow_models.py \
  --workflow ../comfyui-modal-api/exemplos/workflows/sdxl_simple_exampleV2.json \
  --models-root /mnt/runpod-volume/models
```

### Smoke test

```bash
python scripts/smoke_test_endpoint.py \
  --endpoint-id <ENDPOINT_ID> \
  --workflow ../comfyui-modal-api/exemplos/workflows/sdxl_simple_exampleV2.json \
  --save-response /tmp/runpod-smoke-response.json \
  --save-image /tmp/runpod-smoke-output.png
```

### Load test

Recomendacao atual: para medir concorrencia real, use `runsync` concorrente.

```bash
python scripts/load_test_endpoint.py \
  --endpoint-id <ENDPOINT_ID> \
  --workflow ../comfyui-modal-api/exemplos/workflows/sdxl_simple_exampleV2.json \
  --overrides-json /tmp/arrakis_load_overrides.json \
  --request-mode runsync \
  --total-requests 10 \
  --concurrency 10 \
  --wait-for-drain-timeout-s 900 \
  --save-json /tmp/runpod-load-test.json
```

O modo `run` continua disponivel para investigar comportamento async, mas os testes reais desta base mostraram `runsync` mais previsivel sob carga.

## Observacoes reais do Runpod

Pontos validados na pratica:

- volume serverless fica montado em `/runpod-volume`
- ComfyUI espera modelos em `/runpod-volume/models/...`
- o attach multi-volume em endpoint nativo funciona, mas o caminho confiavel foi salvar isso via GraphQL
- a conta precisa de quota suficiente para `workersMax`
- cancelar o cliente local nao cancela automaticamente jobs ja enfileirados no Runpod

Por isso, a estrategia recomendada aqui e:

- usar 1 endpoint global simples para imagem
- usar volumes espelhados por regiao
- usar `runsync` no backend principal
- separar endpoint heavy apenas quando realmente necessario

## Build local

```bash
docker build --platform linux/amd64 -t runpod-comfyui-universal .
python -m unittest discover -s tests -p 'test_handler.py'
```
