FROM python:3.12-slim AS base
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY chappie/ chappie/
COPY cli/ cli/
RUN pip install --no-cache-dir .

EXPOSE 8787
CMD ["python", "-m", "uvicorn", "chappie.api:app", "--host", "0.0.0.0", "--port", "8787"]
