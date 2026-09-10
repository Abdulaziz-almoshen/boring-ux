# Third-party notices

Boring UX is MIT-licensed. It runs these third-party components locally; their licenses apply to them:

- **MediaPipe FaceLandmarker** — Apache-2.0 (Google).
- **whisper.cpp** and the **Whisper large-v3-turbo** weights — MIT (ggerganov / OpenAI).
- **L2CS-Net** code — MIT (Ahmednull; installed fork edavalosanaya). The bundled **L2CS weights are trained on Gaze360**, whose
  license permits **non-commercial research use only**. If you use Boring UX commercially, replace them or use the MediaPipe-only
  gaze signal.
- **WebGazer.js** — GPLv3 (Brown University; LGPLv3 offered to companies under $1M valuation). It is bundled in `extension/`.
- PyTorch (BSD-3), OpenCV (Apache-2.0), PyAV (BSD-3), ffmpeg (LGPL/GPL, invoked as a separate program).

Report writing uses Claude via your own Claude Code subscription or API key.
