FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV APP_ENV=production
EXPOSE 8501
RUN useradd -m appuser
USER appuser
# enableCORS/XsrfProtection은 플랫폼 리버스프록시(Caddy/IIS/Apache) 뒤에서 동작하기 위해 끈다.
CMD ["streamlit", "run", "app.py", \
     "--server.port=8501", "--server.address=0.0.0.0", "--server.headless=true", \
     "--server.enableCORS=false", "--server.enableXsrfProtection=false"]
