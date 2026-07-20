# JJHo — The Fan Almanac. Single container: gunicorn serving the Flask app.
FROM python:3.12-slim

# Non-root runtime user.
RUN useradd --create-home --uid 10001 jjho

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY jjho/ ./jjho/

# The SQLite index (episodes + transcripts) and scrape caches live under
# /app/data — mount a volume there. All of it is re-derivable from public data,
# so no off-box backup is required (see DESIGN.md).
ENV JJHO_DATA=/app/data \
    PYTHONUNBUFFERED=1

RUN mkdir -p /app/data && chown -R jjho:jjho /app

USER jjho
EXPOSE 8080

CMD ["gunicorn", "--workers", "2", "--threads", "8", \
     "--bind", "0.0.0.0:8080", "--timeout", "120", \
     "--access-logfile", "-", "jjho.web:create_app()"]
