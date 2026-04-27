# Anthive theme for MkDocs Material report sites

## Brand colors

| Token | Value | Use |
|---|---|---|
| Primary dark | `#1a1a2e` | Top bar background, MkDocs header |
| Primary medium | `#2a2a4e` | Borders, hover states |
| Accent | `#7dd3fc` | Logo text, highlights |
| Link | `#0055cc` | All hyperlinks |
| Text | `#222` | Body text |
| Page background | `#fafafa` | Main content area |
| Font (mono) | `'Courier New', Courier, monospace` | Anthive browser body, code blocks |
| Font (sans) | System default (`-apple-system, ...`) | MkDocs body text (keep Material default) |

## mkdocs.yml configuration

Add this to your `mkdocs.yml` to match the anthive look:

```yaml
theme:
  name: material
  custom_dir: overrides
  palette:
    - media: "(prefers-color-scheme: light)"
      scheme: default
      primary: custom
      accent: custom
      toggle:
        icon: material/brightness-7
        name: Switch to dark mode
    - media: "(prefers-color-scheme: dark)"
      scheme: slate
      primary: custom
      accent: custom
      toggle:
        icon: material/brightness-4
        name: Switch to light mode

extra_css:
  - assets/anthive-theme.css
```

## CSS file to add

Create `docs/assets/anthive-theme.css` in your MkDocs project:

```css
/* Anthive brand overrides for MkDocs Material */

/* Light mode */
[data-md-color-scheme="default"] {
    --md-primary-fg-color:         #1a1a2e;
    --md-primary-fg-color--light:  #2a2a4e;
    --md-primary-fg-color--dark:   #12122a;
    --md-accent-fg-color:          #7dd3fc;
    --md-typeset-a-color:          #0055cc;
}

/* Dark mode */
[data-md-color-scheme="slate"] {
    --md-primary-fg-color:         #1a1a2e;
    --md-primary-fg-color--light:  #2a2a4e;
    --md-primary-fg-color--dark:   #12122a;
    --md-accent-fg-color:          #7dd3fc;
}

/* Code blocks: monospace matching anthive browser */
.md-typeset code {
    font-family: 'Courier New', Courier, monospace;
    font-size: 12px;
}

/* Tables: compact style matching anthive browser */
.md-typeset table:not([class]) th {
    text-transform: uppercase;
    font-size: 11px;
    letter-spacing: 0.5px;
    color: #666;
    font-weight: normal;
}
```

## Optional: "Back to Anthive" banner

Create `overrides/main.html`:

```html
{% extends "base.html" %}

{% block announce %}
<div style="background:#1a1a2e; color:#e0e0e0; padding:4px 14px; font-size:12px; font-family:'Courier New',monospace; display:flex; align-items:center; gap:10px; border-bottom:1px solid #2a2a4e;">
    <a href="https://data.nevel.eu/" style="color:#7dd3fc; font-weight:bold; text-decoration:none; letter-spacing:0.5px;">anthive</a>
    <span style="color:#444;">|</span>
    <a href="https://data.nevel.eu/" style="color:#aaa; text-decoration:none; font-size:12px;">datasets</a>
    <a href="https://data.nevel.eu/streamlit" style="color:#aaa; text-decoration:none; font-size:12px;">streamlit</a>
    <a href="https://data.nevel.eu/api/docs" style="color:#aaa; text-decoration:none; font-size:12px;">api</a>
</div>
{% endblock %}
```

This adds a slim navigation bar above the MkDocs header that links back to the main anthive site.
