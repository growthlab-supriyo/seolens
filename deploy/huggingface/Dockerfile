# Hugging Face Space (Docker) for the public Simple Scope demo: 500 URLs per crawl, no JavaScript rendering.
# Copy this file and README.md (in this folder) into a new Space; see docs/LAUNCH.md.
FROM python:3.12-slim

# Spaces run the container as user 1000.
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    SIMPLESCOPE_DEMO=1 \
    SIMPLESCOPE_HOME=/home/user/data \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir --user "simple-scope==0.1.0"

EXPOSE 7860
CMD ["simple-scope", "app", "--host", "0.0.0.0", "--port", "7860", "--no-browser"]
