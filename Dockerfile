FROM python:3.11-slim

# Installer FFmpeg + dépendances système
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Installer torch CPU uniquement (beaucoup plus léger)
RUN pip install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cpu

# Installer les autres dépendances
COPY requirements.txt .
RUN pip install --no-cache-dir flask openai-whisper

# Pré-télécharger le modèle Whisper "base" (~139 MB)
RUN python -c "import whisper; whisper.load_model('base')"

# Copier le code de l'app
COPY autocut_studio.py .

# Créer les dossiers de données
RUN mkdir -p autocut_data/uploads autocut_data/exports autocut_data/temp

EXPOSE 5000

ENV PYTHONUNBUFFERED=1

CMD ["python", "autocut_studio.py"]
