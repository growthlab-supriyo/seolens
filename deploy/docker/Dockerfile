# Run SEO Lens in Docker (for example on your own server) like the online version: 500 URLs per crawl, no JavaScript rendering.
# Build: docker build -t seolens deploy/docker   Run: docker run -p 7860:7860 seolens   Open: http://localhost:7860
# Remove SEOLENS_ONLINE=1 below to run it without the online limits.
FROM python:3.12-slim

RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    SEOLENS_ONLINE=1 \
    SEOLENS_HOME=/home/user/data \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir --user "seolens==0.1.0"

EXPOSE 7860
CMD ["seolens", "app", "--host", "0.0.0.0", "--port", "7860", "--no-browser"]
