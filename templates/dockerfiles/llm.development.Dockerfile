# development: --enforce-eager로 CUDA graph 컴파일 생략 (기동 빠름, 처리량 낮음)
FROM vllm/vllm-openai:latest
ARG APP_PROFILE=development
ENV VLLM_ARGS="--enforce-eager --gpu-memory-utilization 0.5"
EXPOSE 8000
