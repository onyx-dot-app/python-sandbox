from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


def test_numpy_pandas_matplotlib_stack() -> None:
    client = TestClient(create_app())

    code = """
import io
import json

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

data = np.arange(6, dtype=np.float64).reshape(3, 2)
df = pd.DataFrame(data, columns=['x', 'y'])

summary = {
    'shape': list(df.shape),
    'x_mean': float(df['x'].mean()),
    'y_total': float(df['y'].sum()),
}

fig, ax = plt.subplots()
ax.plot(df['x'], df['y'])
buf = io.BytesIO()
fig.savefig(buf, format='png')
plt.close(fig)

print(json.dumps({'summary': summary, 'png_bytes': len(buf.getvalue())}))
""".strip()

    response = client.post(
        "/v1/execute",
        json={
            "code": code,
            "stdin": None,
            "timeout_ms": 2000,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    print(f"DEBUG: Full payload: {payload}")
    assert payload["stderr"] == "", f"stderr was: {payload['stderr']}"
    assert payload["exit_code"] == 0, (
        f"exit_code was: {payload['exit_code']}, timed_out: {payload.get('timed_out')}"
    )
    assert payload["timed_out"] is False

    stdout = payload["stdout"].strip()
    result = json.loads(stdout)

    assert result["summary"]["shape"] == [3, 2]
    assert result["summary"]["x_mean"] == pytest.approx(2.0, rel=1e-9)
    assert result["summary"]["y_total"] == pytest.approx(9.0, rel=1e-9)
    assert result["png_bytes"] > 0


def test_matplotlib_creates_graph_and_returns_as_file() -> None:
    client = TestClient(create_app())

    code = """
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

# Create sample data
x = np.linspace(0, 10, 100)
y = np.sin(x)

# Create the plot
fig, ax = plt.subplots(figsize=(8, 6))
ax.plot(x, y, 'b-', linewidth=2, label='sin(x)')
ax.set_xlabel('X axis')
ax.set_ylabel('Y axis')
ax.set_title('Sine Wave')
ax.legend()
ax.grid(True, alpha=0.3)

# Save the figure
fig.savefig('sine_wave.png', dpi=100, bbox_inches='tight')
plt.close(fig)

print('Graph saved successfully')
""".strip()

    response = client.post(
        "/v1/execute",
        json={
            "code": code,
            "stdin": None,
            "timeout_ms": 3000,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["stdout"] == "Graph saved successfully\n"
    assert payload["stderr"] == ""
    assert payload["exit_code"] == 0
    assert payload["timed_out"] is False

    # Verify the PNG file was created and returned
    files = payload.get("files")
    assert isinstance(files, list)

    # Find the sine_wave.png file
    png_file = None
    for file_entry in files:
        if isinstance(file_entry, dict) and file_entry.get("path") == "sine_wave.png":
            png_file = file_entry
            break

    assert png_file is not None, "sine_wave.png not found in response files"
    assert png_file["kind"] == "file"

    # Verify the file has a file_id
    file_id = png_file.get("file_id")
    assert isinstance(file_id, str)

    # Download the file and verify it's a valid PNG
    download_response = client.get(f"/v1/files/{file_id}")
    assert download_response.status_code == 200
    png_bytes = download_response.content

    # PNG files start with these magic bytes
    assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"

    # Verify the file has reasonable size (should be several KB for a plot)
    assert len(png_bytes) > 1000
