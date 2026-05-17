# Image Compressor

A small batch image compressor with both a CLI and a Flask web UI.

## Web UI

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open `http://localhost:8080`.

The web app lets you drag in multiple JPG, PNG, WEBP, BMP, or TIFF files, preview them as thumbnails, tune target size / max dimension / worker count, watch live progress, download each result, or download the whole batch as a ZIP.

Uploaded files are written into session-specific temporary directories under `uploads/`. Compressed outputs are created in a sibling `compressed/` folder inside each session directory.

## Docker

Build and run the container directly:

```bash
mkdir -p uploads
docker build -t img-compress .
docker run --rm -p 8080:8080 -v "$(pwd)/uploads:/app/uploads" img-compress
```

Or run it with Docker Compose:

```bash
mkdir -p uploads
docker compose up --build -d
```

Then open `http://localhost:8080`. The mounted `./uploads` directory keeps uploaded and compressed files on the host between container restarts.

## CLI

```bash
python img_compress.py /path/to/images --target-size 2 --max-dim 4096 --workers 4
```

## Screenshot description

The interface uses a dark SaaS-style layout with a gradient backdrop, glassmorphism cards, a drag-and-drop upload panel, thumbnail grid, right-hand tuning controls, animated progress bar, and a results panel with download actions.
