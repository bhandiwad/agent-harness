FROM python:3.12-slim
WORKDIR /opt/agent
COPY requirements.lock pyproject.toml ./
RUN python -m pip install --no-cache-dir --no-deps -r requirements.lock
COPY src ./src
RUN python -m pip install --no-cache-dir --no-deps . && useradd --uid 10001 --create-home agent
USER 10001:10001
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["pandita-agent"]
CMD ["api", "--config", "/config/agent.json", "--host", "0.0.0.0"]
