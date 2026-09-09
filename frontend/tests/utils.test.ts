import { describe, it, expect } from 'vitest';
import { escapeHtml, sanitizeColor } from '../src/utils';

describe('Security Utilities', () => {
  it('escapeHtml sanitizes script tags and injection payloads', () => {
    const payload = '<script>alert("xss")</script>';
    expect(escapeHtml(payload)).toBe('&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;');

    const imgPayload = '<img src=x onerror=alert(1)>';
    expect(escapeHtml(imgPayload)).toBe('&lt;img src=x onerror=alert(1)&gt;');

    const spaceName = 'Space <img src=x> & "quotes"';
    expect(escapeHtml(spaceName)).toBe('Space &lt;img src=x&gt; &amp; &quot;quotes&quot;');
  });

  it('sanitizeColor rejects dangerous CSS injection', () => {
    expect(sanitizeColor('#3b82f6')).toBe('#3b82f6');
    expect(sanitizeColor('#fff')).toBe('#fff');
    expect(sanitizeColor('red; background: url(javascript:alert(1))')).toBe('#3b82f6');
    expect(sanitizeColor('')).toBe('#3b82f6');
  });
});
