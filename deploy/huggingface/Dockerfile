# Hugging Face Space (Docker) for the public SEO Lens demo: 500 URLs per crawl, no JavaScript rendering.
# Copy this file and README.md (in this folder) into a new Space; see docs/LAUNCH.md.
FROM python:3.12-slim

# Spaces run the container as user 1000.
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    SEOLENS_DEMO=1 \
    SEOLENS_HOME=/home/user/data \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir --user "seolens==0.1.0"

EXPOSE 7860
CMD ["seolens", "app", "--host", "0.0.0.0", "--port", "7860", "--no-browser"]
