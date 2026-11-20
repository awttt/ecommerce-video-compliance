# 管理后台镜像：纯 Python 运行时，不带识别引擎依赖。
# 前端产物在 admin/web/dist 预先构建（npm run build），这里只打包。
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY admin_app ./admin_app
COPY run.py ./
COPY web/dist ./web/dist
EXPOSE 8780
CMD ["python", "run.py"]
