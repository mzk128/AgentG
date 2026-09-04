# 使用官方轻量级的 Python 3.10 镜像
FROM python:3.10-slim

# 设置工作目录
WORKDIR /app

# 复制 requirements.txt 并安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 将项目的所有代码复制到容器中
COPY . .

# 暴露 Web 服务端口
EXPOSE 8000

# 默认启动 Web 服务器
CMD ["python", "-m", "src.multi_agent_system.web_server"]
