FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Fail the image build if a legacy-runtime compatibility dependency is missing
# from the bundle. These files are mounted in some production deployments and
# must also exist in a freshly built image.
RUN python -m py_compile /app/database.py /app/telegram_post_compat.py \
    && python /app/scripts/check_runtime_bundle.py
CMD ["python", "main.py"]
