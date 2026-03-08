FROM runpod/worker-comfyui:5.7.1-base

ARG BAKE_MODELS=false

WORKDIR /build

COPY config/ /build/config/
COPY scripts/ /build/scripts/
COPY handler.py /handler.py

RUN python -m pip install --no-cache-dir boto3
RUN python /build/scripts/install_custom_nodes.py --config /build/config/custom_nodes.json
RUN if [ "${BAKE_MODELS}" = "true" ]; then \
      python -m pip install --no-cache-dir huggingface_hub requests && \
      python /build/scripts/download_models.py --config /build/config/models.json ; \
    else \
      echo "[docker] BAKE_MODELS=false, relying on /runpod-volume/models" ; \
    fi

WORKDIR /
