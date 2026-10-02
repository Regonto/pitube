# PiTube

Lecteur YouTube audio ultra-léger pour Raspberry Pi.  
Front HTML statique + backend Flask/yt-dlp. Pas de vidéo, juste l'audio — idéal pour les petites configs.

## Installation

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
sudo apt install ffmpeg
```

## Lancement

```bash
python3 server.py
```

Ouvrir `pitube.html` dans le navigateur.  
Si le front tourne sur une autre machine que le Pi, éditer la ligne `const API = 'http://localhost:5000'` dans `pitube.html`.

## Mise à jour

yt-dlp doit être maintenu à jour régulièrement, sinon l'extraction audio casse :

```bash
pip install -U yt-dlp
```
