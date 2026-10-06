FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /service
COPY services/human_general/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
# Explicit whitelist: no Google clients, processors or accounting writers.
COPY app/__init__.py app/drive_run_state.py app/conditional_drive_state_v2.py app/human_general_authority.py app/human_general_auth_transport.py app/page_receipt_model.py app/pdf_page_identity.py app/models.py app/receipt_validation.py ./app/
COPY services/human_general/ ./services/human_general/
RUN useradd --system --uid 10001 hga
USER 10001
EXPOSE 8080
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "4", "--timeout", "30", "--access-logfile", "/dev/null", "--error-logfile", "/dev/null", "services.human_general.web:make_application()"]
