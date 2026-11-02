#!/usr/bin/env python3
"""Optional offline smoke test. Requires playwright and a local Chromium browser.

Does NOT connect to AWS, load user data, or call any network endpoint.
It checks document widths with the actual static HTML/CSS and simulated cards.
"""
from pathlib import Path
from shutil import which
try:
    from playwright.sync_api import sync_playwright
except ImportError as exc:
    raise SystemExit('Install playwright locally to run this optional test.') from exc

root = Path(__file__).resolve().parents[1] / 'frontend'
html = (root / 'index.html').read_text(encoding='utf-8')
css = (root / 'styles.css').read_text(encoding='utf-8')
html = html.replace('<link rel="stylesheet" href="styles.css">', '<style>' + css + '</style>')
html = html.replace('<script src="app.js" defer></script>', '')
with sync_playwright() as playwright:
    chrome = which("chromium") or which("google-chrome")
    if not chrome:
        raise SystemExit("Install a local Chromium executable to run the optional browser test.")
    browser = playwright.chromium.launch(headless=True, executable_path=chrome)
    for width in (320, 360, 375, 390, 430, 768):
        page = browser.new_page(viewport={'width': width, 'height': 800}, is_mobile=True, has_touch=True)
        page.set_content(html)
        page.evaluate('''() => {
          document.getElementById('authView').classList.add('hidden');
          document.getElementById('appView').classList.remove('hidden');
          document.getElementById('mobileDock').classList.remove('hidden');
          document.getElementById('accountsSummary').innerHTML =
            '<div class="account-summary-card"><span>Conto corrente</span><strong>€ 2.345,70</strong></div>'.repeat(4);
          document.getElementById('creditCardsRows').innerHTML =
            '<div class="service"><span><strong>Carta di credito personale</strong>' +
            '<small>Chiusura 25, addebito 10 del mese successivo</small></span>' +
            '<span><button class="btn btn-soft">Configura</button></span></div>';
        }''')
        for view in ('home','accounts','calendar','movements','recurring','settings','insights','users','fund','spaces'):
            page.evaluate('''(view) => document.querySelectorAll('.view').forEach(
              el => el.classList.toggle('hidden', el.id !== 'view-' + view))''', view)
            measured = page.evaluate('''() => document.documentElement.scrollWidth''')
            if measured > width + 1:
                raise AssertionError(f'Horizontal page overflow: {view}, viewport={width}, document={measured}')
        page.close()
        print(f'Viewport {width}px: OK')
    browser.close()
