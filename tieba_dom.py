"""Shared first-floor resolver used by readiness checks and native transforms.

No selector is accepted without first-floor evidence.  In particular a post ID
alone does not distinguish the OP from a reply, and DOM order is not evidence.
"""

# Keep priority and content selectors in one place: waiting for one DOM shape
# and transforming another was a source of misleading missing-first-post errors.
FIRST_POST_RESOLVER = r"""
() => {
  const text = node => (node?.textContent || '').replace(/\s+/g, ' ').trim();
  const groups = [
    // Desktop and data-driven layouts: authoritative JSON floor metadata.
    ['field', '[data-field], [data-field-json]'],
    // Mobile/semantic layouts with explicit floor attributes (tag independent).
    ['floor', '[data-floor], [data-post-no]'],
    // Legacy desktop pages without JSON: read ALL floor-tail nodes.
    ['desktop_tail', '.l_post, .j_l_post'],
  ];
  const contentSelectors = [
    '[id^="post_content_"]', '.d_post_content', '.j_d_post_content',
    '.p_content', '[data-role="post-content"]', '.post-content',
  ];
  const floor = node => {
    for (const attr of ['data-field', 'data-field-json']) {
      try {
        const field = JSON.parse(node.getAttribute(attr) || 'null');
        const value = field?.content?.post_no ?? field?.content?.floor ?? field?.post_no ?? field?.floor;
        if (value != null && String(value).trim() !== '') return Number(value);
      } catch (_) {}
    }
    for (const attr of ['data-floor', 'data-post-no']) {
      const value = node.getAttribute(attr);
      if (value != null && value.trim() !== '') return Number(value);
    }
    if (!node.matches('.l_post, .j_l_post')) return null;
    const tails = [...node.querySelectorAll('.tail-info, .post-tail, .p_tail, .j_l_post_num, .d_post_info, .p_props')];
    for (const tail of tails) {
      const match = text(tail).match(/(?:^|\s)(\d+)\s*楼(?:\s|$)/);
      if (match) return Number(match[1]);
    }
    return null;
  };
  const hasBody = node => Boolean(node && (text(node) || node.querySelector('img, video, audio, iframe')));
  const counts = {}, candidates = [], seen = new Set();
  let selected = null, firstFloorSeen = false;
  for (const [name, selector] of groups) {
    const nodes = [...document.querySelectorAll(selector)];
    for (const part of selector.split(',').map(value => value.trim())) counts[part] = document.querySelectorAll(part).length;
    for (const node of nodes) {
      if (!seen.has(node)) { seen.add(node); candidates.push(node); }
      if (floor(node) !== 1) continue;
      firstFloorSeen = true;
      for (const contentSelector of contentSelectors) {
        const content = node.querySelector(contentSelector);
        if (!hasBody(content)) continue;
        if (!selected) selected = {article: node, content, selector: `${name}: ${selector} -> ${contentSelector}`};
        break;
      }
    }
  }
  for (const selector of contentSelectors) counts[selector] = document.querySelectorAll(selector).length;
  return {selected, candidates, counts, firstFloorSeen};
}
"""

PAGE_SNAPSHOT_SCRIPT = r"""
() => {
  const resolved = (__RESOLVER__)();
  const bodyText = document.body?.innerText || '';
  const visible = selector => [...document.querySelectorAll(selector)].some(node => node.getClientRects().length);
  return {
    url: location.href, title: document.title, ready_state: document.readyState,
    html_length: document.documentElement?.outerHTML.length || 0,
    body_text_length: bodyText.length, body_text: bodyText.slice(0, 12000),
    has_first_post: Boolean(resolved.selected), first_floor_seen: resolved.firstFloorSeen,
    selected_selector: resolved.selected?.selector || '', selector_counts: resolved.counts,
    has_post_features: resolved.firstFloorSeen || Boolean(document.querySelector('[id^="post_content_"], #j_p_postlist, .l_post, .j_l_post, [data-pid], [data-role="post-content"]')),
    has_loading_indicator: visible('[aria-busy="true"], [role="progressbar"], .loading, .loading-tip'),
    has_login_form: visible('input[type="password"], form[action*="passport.baidu.com"]'),
    has_verify_widget: visible('#captcha, #vcode, input[name="vcode"], iframe[src*="wappass.baidu.com"], iframe[src*="captcha"]'),
    has_app_gate: visible('a[href^="com.baidu.tieba://"], a[href^="tieba://"]'),
    iframe_count: document.querySelectorAll('iframe').length,
    image_count: document.querySelectorAll('img').length,
  };
}
""".replace("__RESOLVER__", FIRST_POST_RESOLVER)

FIRST_POST_READY_SCRIPT = "() => Boolean((" + FIRST_POST_RESOLVER + ")().selected)"
