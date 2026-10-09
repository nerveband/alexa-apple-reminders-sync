FROM python:3.12-slim
ENV SETUPTOOLS_SCM_PRETEND_VERSION_FOR_PYICLOUD=2.7.1.dev0
RUN pip install --no-cache-dir alexapy==1.31.0 \
    "pyicloud @ https://github.com/timlaing/pyicloud/archive/21893e1e5cd01c0b78080912db9ac72c5f20456e.tar.gz" \
 && useradd -u 10001 -m appuser
WORKDIR /app
COPY app.py alexa_login.py ./
USER appuser
ENV PYTHONUNBUFFERED=1
CMD ["python", "app.py"]
