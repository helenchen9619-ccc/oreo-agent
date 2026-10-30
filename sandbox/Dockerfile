FROM python:3.12-slim

# 国内服务器构建慢/卡死时传 --build-arg PIP_INDEX=https://mirrors.aliyun.com/pypi/simple/
ARG PIP_INDEX=""

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir ${PIP_INDEX:+--index-url $PIP_INDEX} -r requirements.txt

COPY . .

EXPOSE 8100
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]
