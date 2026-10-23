# ADMA Studio script runner: runs users' Python scripts in isolation.
# Add packages that scripts need to the pip line, then rebuild: docker compose build studio-runner
FROM python:3.11-slim
RUN pip install --no-cache-dir numpy pandas scipy matplotlib openpyxl pillow
RUN useradd --uid 10001 --create-home runner
COPY runner.py /opt/runner/runner.py
USER 10001
CMD ["python", "-u", "/opt/runner/runner.py"]
