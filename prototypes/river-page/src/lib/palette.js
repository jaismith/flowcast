// One palette per theme. applyTheme() copies the chosen one into `C` (read by Plot and MapLibre) and into the
// --color-* CSS variables Tailwind's utilities use, so the whole page recolors from this file.

const FLOOD = { action: '#f2c200', minor: '#f08c00', moderate: '#e03131', major: '#ae3ec9' };

export const THEMES = {
  modern: {
    label: 'Modern',
    basemap: 'positron',
    paper: '#f6f5f1',
    card: '#ffffff',
    ink: '#16191d',
    muted: '#6b7079',
    faint: '#a3a6ad',
    line: '#e6e3dc',
    normal: '#ece8df',
    flow: '#1f4e79',
    rain: '#3d7fe8',
    snow: '#a48bf0',
    melt: '#26a69a',
    sun: '#f0a202',
    alert: '#d9480f',
    ...FLOOD,
  },
  classic: {
    label: 'Classic',
    basemap: 'bright',
    paper: '#d8e1ea',
    card: '#ffffff',
    ink: '#000000',
    muted: '#4a4a4a',
    faint: '#8a8a8a',
    line: '#a9b4bf',
    normal: '#ecebd2',
    flow: '#003399',
    rain: '#3366cc',
    snow: '#9966cc',
    melt: '#339999',
    sun: '#ff9900',
    alert: '#cc0000',
    ...FLOOD,
    moderate: '#cc0000',
    major: '#990099',
  },
  ascii: {
    label: 'ASCII',
    basemap: 'dark',
    paper: '#0d0f0e',
    card: '#121614',
    ink: '#d9e4de',
    muted: '#86948d',
    faint: '#55615b',
    line: '#27302c',
    normal: '#1d2522',
    flow: '#7ee2b8',
    rain: '#6cb6ff',
    snow: '#c4a7ff',
    melt: '#4fd1c5',
    sun: '#ffcc66',
    alert: '#ff7b54',
    ...FLOOD,
  },
  serif: {
    label: 'Serif',
    basemap: 'positron',
    paper: '#fbfaf6',
    card: '#fbfaf6',
    ink: '#1b1b1b',
    muted: '#62605a',
    faint: '#a29f96',
    line: '#ddd8cc',
    normal: '#eeeadf',
    flow: '#1b365d',
    rain: '#4a78b0',
    snow: '#8c7bbd',
    melt: '#3b8a7c',
    sun: '#c98a12',
    alert: '#b5401c',
    ...FLOOD,
  },
};

export const C = { name: 'modern', ...THEMES.modern };

const CSS_KEYS = ['paper', 'card', 'ink', 'muted', 'faint', 'line', 'normal', 'flow', 'rain', 'snow', 'melt', 'sun', 'alert', 'action', 'minor', 'moderate', 'major'];

export function applyTheme(name) {
  const theme = THEMES[name] ? name : 'modern';
  Object.assign(C, THEMES[theme], { name: theme });
  const root = document.documentElement;
  root.dataset.theme = theme;
  for (const k of CSS_KEYS) root.style.setProperty(`--color-${k}`, C[k]);
  return theme;
}
