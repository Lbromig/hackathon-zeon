// Top-down world map (backend/app/api/calibration.py, core/viz/scene.py).
//
// Two views of the same thing: `scene` is the data, `scene.svg` is the server's
// own rendering of it. The tab shows the server SVG rather than re-plotting the
// JSON, so the picture on screen is byte-identical to the one you can drop into a
// slide — one renderer, no drift.

/** Positions are world-frame metres; heading is the optical axis, radians, +X→+Y. */
export interface SceneCamera {
  id: string;
  x: number;
  y: number;
  z: number;
  heading: number;
}

export interface SceneEntity {
  id: string;
  kind: string;
  x: number;
  y: number;
  z: number;
}

export interface Scene {
  cameras: SceneCamera[];
  entities: SceneEntity[];
}

/** Thrown for 409 "not calibrated yet" — a normal state, not a failure. */
export class NotCalibrated extends Error {}

async function guard(r: Response): Promise<Response> {
  if (r.status === 409) {
    let detail = "not calibrated yet";
    try {
      detail = (await r.json()).detail ?? detail;
    } catch {
      /* keep the default */
    }
    throw new NotCalibrated(detail);
  }
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r;
}

export const getScene = async (): Promise<Scene> =>
  (await guard(await fetch("/api/worldmodel/scene"))).json();

/** The SVG as markup, so it can be inlined and themed rather than boxed in an <img>. */
export const getSceneSvg = async (): Promise<string> =>
  (await guard(await fetch("/api/worldmodel/scene.svg"))).text();

export const SCENE_SVG_URL = "/api/worldmodel/scene.svg";
