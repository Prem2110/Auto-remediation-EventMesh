import { useEffect, useState } from "react";

interface ThemeColors {
  primary:    string;
  chart1:     string;
  chart2:     string;
  chart3:     string;
  chart4:     string;
  chart5:     string;
  chart6:     string;
  chart7:     string;
  cardBg:     string;
  cardBorder: string;
  textPrimary:string;
  bgHover:    string;
}

function read(): ThemeColors {
  const s = getComputedStyle(document.documentElement);
  const v = (name: string, fallback: string) =>
    s.getPropertyValue(name).trim() || fallback;
  return {
    primary:    v("--orbit-primary",     "#2563eb"),
    chart1:     v("--orbit-chart-1",     "#ff6b6b"),
    chart2:     v("--orbit-chart-2",     "#4dabf7"),
    chart3:     v("--orbit-chart-3",     "#ffd43b"),
    chart4:     v("--orbit-chart-4",     "#69db7c"),
    chart5:     v("--orbit-chart-5",     "#845ef7"),
    chart6:     v("--orbit-chart-6",     "#f06595"),
    chart7:     v("--orbit-chart-7",     "#74c0fc"),
    cardBg:     v("--orbit-card-bg",     "#ffffff"),
    cardBorder: v("--orbit-card-border", "#e2e8f0"),
    textPrimary:v("--orbit-text-primary","#1e293b"),
    bgHover:    v("--orbit-bg-hover",    "#f8fafc"),
  };
}

export function useThemeColors(): ThemeColors {
  const [colors, setColors] = useState<ThemeColors>(read);

  useEffect(() => {
    const obs = new MutationObserver(() => setColors(read()));
    obs.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });
    return () => obs.disconnect();
  }, []);

  return colors;
}
