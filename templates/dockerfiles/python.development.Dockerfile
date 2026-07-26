FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV APP_ENV=development
EXPOSE 8000
# --reload: 코드 변경 즉시 반영 (디버깅용, 단일 프로세스)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
