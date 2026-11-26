# The MicroVM image: Python, git, pytest, and the durable-execution compute worker.
# Lambda runs this Dockerfile once at build time, starts app.py, and snapshots
# the running process after the ready hook. Every MicroVM resumes that snapshot.
FROM public.ecr.aws/docker/library/python:3.13-alpine
RUN apk add --no-cache git
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt
COPY app.py .
EXPOSE 8080
CMD ["python", "-u", "app.py"]
