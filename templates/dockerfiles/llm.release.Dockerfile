FROM vllm/vllm-openai:latest
ARG APP_PROFILE=release
ENV VLLM_ARGS="--gpu-memory-utilization 0.9"
EXPOSE 8000
