FROM python:3.11-slim
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.revision=${SOURCE_REVISION}
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Fail the image build if a legacy-runtime compatibility dependency is missing
# from the bundle. These files are mounted in some production deployments and
# must also exist in a freshly built image.
RUN python -m py_compile /app/database.py /app/telegram_post_compat.py \
    && python /app/scripts/check_runtime_bundle.py
USER 10001:10001
CMD ["python", "main.py"]
