"""Trusted finite interaction contract, run outside the model checkout.

Uses the Web renderer verbatim and RuntimePreviews for the exact output commit.
No tests or scripts supplied by the model are used as the oracle.
"""
import base64
import json
import re
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from timetrace import app_preview, runtime_preview

DEFAULT_SELECTORS = {
    'input': '#todoInput', 'add': '#addTask', 'rows': '.todo-item',
    'toggle': 'input[type="checkbox"]', 'all': '[data-filter="all"]',
    'active': '[data-filter="active"]', 'completed': '[data-filter="completed"]',
    'status': '#todoStatus',
}


def selectors(raw):
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != set(DEFAULT_SELECTORS):
            raise ValueError()
        if any(not isinstance(v, str) or not v.strip() or len(v) > 200 or '\0' in v for v in value.values()):
            raise ValueError()
        return value
    except (ValueError, TypeError):
        raise ValueError('invalid_config') from None


def prepare():
    """Web config ACK requires an actual browser launch and Docker availability."""
    try:
        from playwright.sync_api import sync_playwright
        runtime_preview.command(['docker', 'info', '--format', '{{.ServerVersion}}'])
        with sync_playwright() as p:
            if not Path(p.chromium.executable_path).is_file():
                subprocess.run([sys.executable, '-I', '-m', 'playwright', 'install', 'chromium', '--no-shell'],
                               check=True, capture_output=True, timeout=120)
            browser = p.chromium.launch(channel='chromium', headless=True, chromium_sandbox=True)
            browser.close()
    except Exception:
        raise ValueError('browser_runtime_unavailable') from None


PARENT = '''async ({bundle, nonce}) => {
  const doc = await KejiPreview.applicationDocument(bundle, nonce, true);
  window.addEventListener('message', async e => {
    const f = document.querySelector('iframe');
    if(e.source !== f.contentWindow || e.data?.kejiPreview !== nonce || e.data.kind !== 'http') return;
    const q = e.data.detail;
    let result;
    try { result = await window.kejiRelay(q); } catch(e) { result = {error: String(e)}; }
    f.contentWindow.postMessage({kejiPreview:nonce,kind:'http-response',detail:{...result,id:q.id}}, '*');
  });
  const f = document.createElement('iframe');
  f.setAttribute('sandbox','allow-scripts');f.title='真实应用预览';
  f.style='width:100%;height:900px;border:0';f.srcdoc=doc;document.body.append(f);
}'''


def run(spec, evidence_dir=None):
    from playwright.sync_api import sync_playwright, expect, TimeoutError as BrowserTimeout
    cfg = selectors(spec['value'])
    root = Path.cwd()
    commit = runtime_preview.command(['git', 'rev-parse', 'HEAD'], cwd=root).decode().strip()
    report = {'suite':'todo_browser_v1', 'commit':commit, 'steps':[], 'http':[], 'status':'error',
              'renderer_source_sha256':Path(__file__).with_name('preview_renderer.sha256').read_text().strip()}
    key = uuid.uuid4().hex
    agent = SimpleNamespace(db=SimpleNamespace(get_workspace=lambda _: {'path':str(root)}), _stop=threading.Event())
    previews = runtime_preview.RuntimePreviews(agent)
    browser = None
    playwright = None
    page = None
    phase = '启动隔离后端'
    def passed(label):
        report['steps'].append({'name':label,'status':'passed'})
        print('PASS '+label, flush=True)
    try:
        body, diagnostic = app_preview.collect(str(root), commit)
        if not body:
            raise AssertionError(diagnostic or '当前提交缺少可预览的 index.html')
        bundle = json.loads(body)
        if bundle['entry'] != spec['path'] or not bundle.get('backend'):
            raise AssertionError('交互保存验收需要 index.html 与 .keji/preview.json 隔离后端配置')
        # The infrastructure probe distinguishes an unavailable computer from an app failure.
        runtime_preview.command(['docker','info','--format','{{.ServerVersion}}'])
        try:
            previews.launch({'id':key, 'workspace_id':'check', 'commit':commit, 'lifetime_seconds':180})
        except ValueError as e:
            if '镜像准备失败' in str(e) or 'Docker' in str(e): raise
            raise AssertionError(str(e)) from e
        initial = previews.http(key, {'method':'GET','path':'/'})
        if initial['code'] != 200 or 'text/html' not in initial['content_type']:
            raise AssertionError('后端入口 / 必须返回 200 HTML')
        for f in bundle['files']:
            if f['path'] == bundle['entry']: f['data'] = initial['body']
        playwright = sync_playwright().start()
        p = playwright
        phase = '启动验收浏览器'
        browser = p.chromium.launch(channel='chromium', headless=True, chromium_sandbox=True)
        context = browser.new_context(service_workers='block', accept_downloads=False, viewport={'width':1280,'height':1000})
        # Only data/srcdoc resources and the source-checked bridge are available.
        context.route('**/*', lambda route: route.abort())
        def open_page():
            tab = context.new_page()
            tab.set_default_timeout(8000)
            def relay(source, q):
                if source['frame'] != tab.main_frame: raise ValueError('invalid bridge caller')
                if len(report['http']) >= 100: raise ValueError('request budget exceeded')
                if not isinstance(q, dict) or len(q.get('body','')) > 44000: raise ValueError('invalid request')
                r = previews.http(key,q)
                report['http'].append({'method':q['method'],'path':q['path'][:200],'code':r['code']})
                return r
            tab.expose_binding('kejiRelay', relay)
            tab.set_content('<!doctype html><html><body></body></html>')
            tab.add_script_tag(content=Path(__file__).with_name('preview_renderer.js').read_text())
            try: tab.evaluate(PARENT, {'bundle':bundle,'nonce':uuid.uuid4().hex})
            except Exception as e: raise AssertionError('预览渲染失败：'+str(e)[:300]) from e
            return tab, tab.frame_locator('iframe')
        page, frame = open_page()
        phase = '载入任务清单'
        inp = frame.locator(cfg['input'])
        expect(inp).to_be_enabled(timeout=10000)
        a,b = '自动验收 A '+key[:8], '自动验收 B '+key[:8]
        row = lambda title: frame.locator(cfg['rows']).filter(has_text=title)
        phase = '点击新增后应显示且只新增一条任务'
        inp.fill(a); frame.locator(cfg['add']).click()
        expect(row(a)).to_have_count(1, timeout=10000)
        expect(row(a)).to_be_visible()
        passed('点击新增')
        phase = 'Enter 新增后应显示且不重复'
        inp.fill(b); inp.press('Enter')
        expect(row(b)).to_have_count(1, timeout=10000)
        expect(row(b)).to_be_visible()
        passed('Enter 新增')
        phase = '完成与筛选'
        row(a).locator(cfg['toggle']).check()
        expect(row(a).locator(cfg['toggle'])).to_be_checked()
        frame.locator(cfg['completed']).click()
        expect(row(a)).to_be_visible(); expect(row(b)).to_have_count(0)
        frame.locator(cfg['active']).click()
        expect(row(b)).to_be_visible(); expect(row(a)).to_have_count(0)
        frame.locator(cfg['all']).click()
        expect(row(a)).to_be_visible(); expect(row(b)).to_be_visible()
        passed('完成与全部／未完成／已完成筛选')
        phase = '关闭页面再打开，重新读取新增和完成状态'
        page.close(); page, frame = open_page()
        expect(frame.locator(cfg['input'])).to_be_enabled(timeout=10000)
        expect(row(a)).to_have_count(1); expect(row(b)).to_have_count(1)
        expect(row(a).locator(cfg['toggle'])).to_be_checked()
        expect(row(b).locator(cfg['toggle'])).not_to_be_checked()
        passed('刷新后新增与完成状态保存')
        phase = '恢复未完成并复查筛选'
        row(a).locator(cfg['toggle']).uncheck()
        frame.locator(cfg['completed']).click()
        expect(row(a)).to_have_count(0); expect(row(b)).to_have_count(0)
        frame.locator(cfg['active']).click()
        expect(row(a)).to_be_visible(); expect(row(b)).to_be_visible()
        passed('恢复未完成与空筛选')
        phase = '空标题应显示提示且不能发送写入'
        count = len([r for r in report['http'] if r['method']=='POST'])
        frame.locator(cfg['input']).fill('   ')
        frame.locator(cfg['add']).click()
        expect(frame.locator(cfg['status'])).to_contain_text(re.compile('输入|不能为空|标题'))
        # Yield to delayed handlers, then reopen to detect hidden duplicates.
        page.wait_for_timeout(400)
        assert count == len([r for r in report['http'] if r['method']=='POST']), '空标题发出了写入请求'
        page.close(); page, frame = open_page()
        expect(frame.locator(cfg['input'])).to_be_enabled(timeout=10000)
        expect(row(a)).to_have_count(1); expect(row(b)).to_have_count(1)
        expect(row(a).locator(cfg['toggle'])).not_to_be_checked()
        assert len([r for r in report['http'] if r['method']=='POST']) == 2, '新增请求重复或没有保存到后端'
        assert any(r['method'] in ('PATCH','PUT') for r in report['http']), '完成状态未写回后端'
        assert all(r['code'] < 400 for r in report['http']), '交互中后端请求失败'
        passed('空输入、重复提交与恢复状态保存')
        report['status'] = 'passed'
    except (AssertionError, BrowserTimeout) as e:
        report.update(status='error' if phase == '启动验收浏览器' else 'failed', phase=phase, diagnostic=str(e)[:1500])
    except Exception as e:
        report.update(status='error', phase=phase, diagnostic=type(e).__name__+': '+str(e)[:600])
    finally:
        # Screenshots are diagnostic only; objective verdicts come from trusted assertions.
        if evidence_dir:
            evidence = Path(evidence_dir); evidence.mkdir(parents=True, exist_ok=True)
            try:
                if page and not page.is_closed(): page.screenshot(path=str(evidence/'preview.png'), full_page=True)
            except Exception: pass
        if browser:
            try: browser.close()
            except Exception: pass
        if playwright:
            try: playwright.stop()
            except Exception: pass
        if not previews.stop(key):
            report.update(status='error', diagnostic='验收隔离实例清理失败；实例硬期限 30 分钟')
        if evidence_dir: (Path(evidence_dir)/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    return report


def main(spec):
    try:
        report = run(spec)
    except Exception as e:
        print('浏览器验收运行时不可用: '+type(e).__name__, flush=True)
        sys.exit(2)
    print('真实预览交互验收 '+json.dumps(report,ensure_ascii=False), flush=True)
    if report['status'] != 'passed':
        print('请依据失败步骤修复应用并提交；不得改验收器。预览 sandbox=allow-scripts，禁止原生表单提交；请使用显式 click/keydown 调用 fetch。', flush=True)
        sys.exit(1 if report['status']=='failed' else 2)
