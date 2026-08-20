// Full background themes. Each swaps the WHOLE palette (background, panels,
// borders, text and accent) — not just the accent. Base layout stays the same.
export const THEMES = {
  clay: {
    label: "Warm",
    "--bg": "#faf9f5", "--panel": "#ffffff", "--panel2": "#f0ede4", "--line": "#e7e3d9",
    "--text": "#2d2b26", "--dim": "#78756c", "--accent": "#c96442", "--accent2": "#b4502f",
  },
  white: {
    label: "White",
    "--bg": "#f6f7f9", "--panel": "#ffffff", "--panel2": "#eef0f3", "--line": "#e2e5ea",
    "--text": "#1f2328", "--dim": "#6b7280", "--accent": "#4b5563", "--accent2": "#374151",
  },
  green: {
    label: "Green",
    "--bg": "#f1f7f3", "--panel": "#ffffff", "--panel2": "#e4f0e8", "--line": "#d3e5da",
    "--text": "#1e2b23", "--dim": "#5f7268", "--accent": "#2f8f5b", "--accent2": "#236f45",
  },
  blue: {
    label: "Blue",
    "--bg": "#f1f6fb", "--panel": "#ffffff", "--panel2": "#e4eef7", "--line": "#d3e2f0",
    "--text": "#1c2530", "--dim": "#5f6b7a", "--accent": "#3b7ea1", "--accent2": "#2f6685",
  },
  purple: {
    label: "Purple",
    "--bg": "#f6f4fb", "--panel": "#ffffff", "--panel2": "#ede7fa", "--line": "#e0d7f2",
    "--text": "#241f30", "--dim": "#6c6580", "--accent": "#7c5cbf", "--accent2": "#63479f",
  },
};
export const THEME_NAMES = Object.keys(THEMES);

const KEY = "shabd_theme";

export function currentTheme() {
  const saved = localStorage.getItem(KEY) || localStorage.getItem("shabd_accent");
  return THEMES[saved] ? saved : "clay";
}

export function applyTheme(name) {
  const theme = THEMES[name] || THEMES.clay;
  const root = document.documentElement;
  for (const [k, v] of Object.entries(theme)) {
    if (k.startsWith("--")) root.style.setProperty(k, v);
  }
  localStorage.setItem(KEY, name);
}
