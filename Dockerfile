# Optional deployment method. The tool also runs fine WITHOUT Docker:
#   pip install -r requirements.txt && python run.py --batch <file>
FROM python:3.12-slim

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 7860

# Default: launch the Gradio interface. Override to run the CLI, e.g.:
#   docker run --rm -v $(pwd):/app prepay-validator \
#       python run.py --batch data/payment_batch.csv
CMD ["python", "app.py"]
