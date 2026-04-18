import fitz
import base64
import json
import os
import time
from pathlib import Path
from dotenv import load_dotenv
from google import genai

load_dotenv()
client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))

def pdf_to_images(pdf_path, out_dir="data/slides"):
    doc = fitz.open(pdf_path)
    paths = []
    for i, page in enumerate(doc):
        pix = page.get_pixmap(dpi=150)
        p = f"{out_dir}/slide_{Path(pdf_path).stem}_{i:03d}.png"
        pix.save(p)
        paths.append(p)
    print(f"Converted {len(paths)} slides from {pdf_path}")
    return paths

def describe_slide(img_path, retries=5):
    with open(img_path, "rb") as f:
        img_data = base64.b64encode(f.read()).decode()
    for attempt in range(retries):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[
                    {"role": "user", "parts": [
                        {"inline_data": {"mime_type": "image/png", "data": img_data}},
                        {"text": "Describe this lecture slide in detail: main topic, key concepts, formulas, diagrams. Be thorough."}
                    ]}
                ]
            )
            return response.text
        except Exception as e:
            print(f"Attempt {attempt+1} failed: {e}")
            time.sleep(10)
    return "Description unavailable"

def process_all_pdfs(slides_dir="data/slides"):
    # Load existing results to avoid reprocessing
    output_path = "data/descriptions/slides.json"
    if os.path.exists(output_path):
        with open(output_path) as f:
            results = json.load(f)
        done_paths = {r["image_path"] for r in results}
        print(f"Resuming — {len(results)} slides already done")
    else:
        results = []
        done_paths = set()

    pdf_files = list(Path(slides_dir).glob("*.pdf"))
    print(f"Found {len(pdf_files)} PDF files")

    for pdf in pdf_files:
        images = pdf_to_images(str(pdf), slides_dir)
        for i, img_path in enumerate(images):
            if img_path in done_paths:
                print(f"Skipping {img_path} (already done)")
                continue
            print(f"Describing slide {i+1}/{len(images)}: {img_path}")
            text = describe_slide(img_path)
            results.append({
                "id": len(results),
                "pdf": pdf.name,
                "slide_num": i,
                "image_path": img_path,
                "text": text
            })
            # Save after every slide
            os.makedirs("data/descriptions", exist_ok=True)
            with open(output_path, "w") as f:
                json.dump(results, f, ensure_ascii=False, indent=2)
            time.sleep(2)

    print(f"Done! Described {len(results)} slides total")
    return results

if __name__ == "__main__":
    process_all_pdfs()