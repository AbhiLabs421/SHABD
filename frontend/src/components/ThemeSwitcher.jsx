import { useState } from "react";
import { THEMES, THEME_NAMES, applyTheme, currentTheme } from "../lib/theme.js";

// Full background themes in the sidebar. Each swatch previews the theme's
// background + accent; click to recolour the whole UI. Choice persists.
export default function ThemeSwitcher() {
  const [active, setActive] = useState(currentTheme());
  const pick = (name) => {
    applyTheme(name);
    setActive(name);
  };
  return (
    <div style={{ padding: "10px 20px" }}>
      <div className="nav-section" style={{ padding: "0 0 8px" }}>Theme</div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
        {THEME_NAMES.map((name) => {
          const t = THEMES[name];
          const on = active === name;
          return (
            <button
              key={name}
              title={t.label}
              onClick={() => pick(name)}
              style={{
                width: 30,
                height: 30,
                padding: 0,
                borderRadius: 8,
                background: t["--bg"],
                border: on ? `2px solid ${t["--accent"]}` : "1px solid var(--line)",
                cursor: "pointer",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                boxShadow: on ? `0 0 0 2px ${t["--accent"]}33` : "none",
              }}
            >
              <span style={{ width: 12, height: 12, borderRadius: "50%", background: t["--accent"] }} />
            </button>
          );
        })}
      </div>
    </div>
  );
}
