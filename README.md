# Runpod ComfyUI Universal

Base de producao para rodar ComfyUI no Runpod Serverless com foco em:

- workflows arbitrarios
- overrides dinamicos de prompt, seed e steps
- assets por base64 ou URL
- outputs genericos: imagem, video, gif, audio, texto e JSON
- custom nodes pinados por commit
- modelos no Network Volume por padrao
- provisionamento automatizado de template, endpoint, auth de registry, secrets e network volume

## Estrutura

```text
Dockerfile
handler.py
config/
  custom_nodes.json
  custom_nodes.ltx-lipsync.example.json
  models.json
  models.sdxl.example.json
  models.flux-dev.example.json
  models.ltx-lipsync.example.json
  deploy.example.json
  deploy.image.example.json
  deploy.heavy.example.json
scripts/
  install_custom_nodes.py
  download_models.py
  sync_models_to_volume.py
  audit_workflow_models.py
  provision_runpod.py
tests/
```

## Arquitetura

Esta base usa `runpod/worker-comfyui:5.7.1-base` como imagem upstream e substitui o `handler.py`.

O handler novo cobre o que normalmente falta para um SaaS:

1. aceita `overrides` ou `inputs` para mutar workflow sem remontar JSON no backend
2. aceita `assets`, `files`, `media` e `images` por `base64` ou `url`
3. coleta outputs alem de `images`
4. sobe resultado em storage S3/R2 compativel quando o payload fica grande

## Estrategia correta para producao

Para o seu caso, a separacao certa e esta:

- imagem Docker: ComfyUI base + handler universal + custom nodes
- Network Volume: checkpoints, LoRAs, VAE e demais modelos
- endpoint serverless: um pool barato para image e outro separado para heavy/video

Isso reduz rebuild, reduz custo e permite trocar modelos sem republicar a imagem.

## Contrato da API

O endpoint continua seguindo o padrao do Runpod:

- `POST /run`
- `POST /runsync`
- `GET /status/{job_id}`

Payload esperado dentro de `input`:

```json
{
  "input": {
    "workflow": {
      "6": {
        "inputs": {
          "text": "cinematic portrait"
        },
        "class_type": "CLIPTextEncode"
      }
    },
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

Aliases suportados:

- `overrides` ou `inputs`
- `assets`, `files`, `media` ou `images`
- em `images`, o campo `image` e aceito como alias de `data`

`output_mode`:

- `auto`: usa object storage se estiver configurado, senao volta inline/base64
- `inline` ou `base64`: sempre retorna base64
- `object_store` ou `s3`: sempre retorna URL do storage

## Object Storage

Para resultados grandes, configure R2/S3 compativel via env vars:

```bash
S3_ENDPOINT_URL=https://<account-id>.r2.cloudflarestorage.com
S3_BUCKET_NAME=comfyui-outputs
S3_ACCESS_KEY_ID=...
S3_SECRET_ACCESS_KEY=...
S3_REGION=auto
S3_PRESIGN_TTL_SECONDS=86400
```

Se quiser URL publica sem presign:

```bash
S3_PUBLIC_BASE_URL=https://cdn.example.com
```

## Modelos

### Opcao 1: Network Volume

Esse e o caminho padrao deste projeto.

Vantagens:

- imagem Docker menor
- cold start menor
- modelos mutaveis sem rebuild
- custo melhor quando voce usa varios modelos, LoRAs e workflows

Estrutura esperada:

```text
/runpod-volume/models/checkpoints/
/runpod-volume/models/loras/
/runpod-volume/models/vae/
/runpod-volume/models/controlnet/
...
```

Em documentacao oficial consultada em `2026-03-08`, os datacenters com API S3 compativel para Network Volume eram: `EUR-IS-1`, `EU-RO-1`, `EU-CZ-1`, `US-KS-2` e `US-CA-2`.

Para semear o volume:

```bash
export RUNPOD_VOLUME_ACCESS_KEY_ID=...
export RUNPOD_VOLUME_SECRET_ACCESS_KEY=...

python scripts/sync_models_to_volume.py \
  --config config/models.sdxl.example.json \
  --volume-id <NETWORK_VOLUME_ID> \
  --data-center-id US-KS-2
```

Observacao importante: essas credenciais de volume ainda precisam ser criadas no painel do Runpod. Nao encontrei endpoint publico para criar isso por API.

### Opcao 2: Bake no container

Nao e o default, mas continua suportado.

Preencha `config/models.json` e rode:

```bash
docker build --platform linux/amd64 --build-arg BAKE_MODELS=true -t runpod-comfyui-universal .
```

## Custom Nodes

Edite `config/custom_nodes.json`.

Exemplo pinado por commit:

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

Para nodes do registry:

```json
{
  "nodes": [
    {
      "type": "registry",
      "id": "comfyui-kjnodes"
    }
  ]
}
```

## Build local

```bash
docker build --platform linux/amd64 -t runpod-comfyui-universal .
```

A build padrao nao baixa modelos. Ela assume que os modelos vao vir de `/runpod-volume/models`.

## Auditoria de workflow

Antes de apontar um workflow para producao, valide se todos os modelos existem no espelho local do volume:

```bash
python scripts/audit_workflow_models.py \
  --workflow ../comfyui-modal-api/exemplos/workflows/ltx_lip_sync/ltx_lipSync_v2.json \
  --models-root /mnt/runpod-volume/models
```

Se esse script retornar codigo `1`, faltam arquivos no volume.

## Provisionamento no Runpod

Perfis incluidos:

- `config/deploy.image.example.json`
- `config/deploy.heavy.example.json`

Passos:

1. copie um dos arquivos para o config final
2. ajuste `template.image_name`
3. exporte a API key do Runpod

```bash
export RUNPOD_API_KEY=...
```

4. se for usar outputs em R2/S3:

```bash
export R2_ACCESS_KEY_ID=...
export R2_SECRET_ACCESS_KEY=...
```

5. se a imagem for privada no GHCR:

```bash
export GHCR_USERNAME=seu-usuario-github
export GHCR_TOKEN=seu-token-com-read-packages
```

6. rode:

```bash
python scripts/provision_runpod.py --config config/deploy.image.example.json
```

O script cria ou atualiza:

- auth de registry do Runpod, se configurada
- secrets do Runpod
- network volume
- template serverless
- endpoint serverless

Ele tambem valida um detalhe importante: quando existe `network_volume`, o endpoint fica preso ao mesmo `data_center_id` do volume.

Saida esperada:

```json
{
  "template_id": "...",
  "endpoint_id": "...",
  "endpoint_url": "https://api.runpod.ai/v2/...",
  "network_volume_id": "...",
  "network_volume_data_center_id": "US-KS-2",
  "network_volume_s3_endpoint": "https://s3api-us-ks-2.runpod.io",
  "container_registry_auth_id": "..."
}
```

## Publicacao da imagem

O workflow `.github/workflows/docker-publish.yml` publica em `ghcr.io` no push para `main` e usa cache do GitHub Actions para acelerar rebuild.

Fluxo recomendado:

1. suba este diretorio como um repositorio GitHub proprio
2. deixe o pacote no GHCR publico ou use `container_registry_auth` no Runpod
3. use `latest` ou uma tag fixa no arquivo de deploy

## Recomendacao de custo

Para image generation leve, mantenha `workers_min=0`, `flashboot=true` e um endpoint separado com GPUs de 24 GB.

Para workflows pesados, video, Flux dev maior ou stacks de custom nodes mais agressivos, use um endpoint separado com 48 GB ou mais.

Em consulta direta a API do Runpod em `2026-03-08`, os menores precos `uninterruptable` disponiveis para GPUs uteis aqui estavam aproximadamente:

- `NVIDIA RTX A5000`: `0.16/h`
- `NVIDIA RTX A4500`: `0.19/h`
- `NVIDIA A40`: `0.35/h`
- `NVIDIA GeForce RTX 4090`: `0.34/h`
- `NVIDIA L40S`: `0.79/h`

Esses valores mudam. Use como referencia, nao como contrato.

## Estrategia recomendada para o seu SaaS

Nao tente enfiar tudo num endpoint unico.

Use o mesmo codigo com dois endpoints:

- `comfy-image`: pool barato, workflows de imagem
- `comfy-heavy`: pool 48 GB+, video e workflows pesados

Seu backend roteia por `workflow family` e mantem uma API unica para o frontend.

## Exemplos incluidos

Arquivos prontos para adaptar a partir do que voce ja tinha no `comfyui-modal-api`:

- `config/models.sdxl.example.json`
- `config/models.flux-dev.example.json`
- `config/models.ltx-lipsync.example.json`
- `config/custom_nodes.ltx-lipsync.example.json`

## Testes

```bash
python -m unittest tests.test_handler
```
