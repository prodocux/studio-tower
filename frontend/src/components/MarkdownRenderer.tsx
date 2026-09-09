import { JSX } from 'preact';
import { Citation } from '../types';
import { CitationBadge } from './CitationBadge';

interface MarkdownRendererProps {
  content: string;
  citations?: Citation[];
  onSelectCitation?: (cit: Citation) => void;
}

interface MarkdownBlock {
  type: 'codeblock' | 'heading' | 'blockquote' | 'hr' | 'ol' | 'ul' | 'paragraph';
  level?: number;
  lang?: string;
  code?: string;
  content?: string;
  items?: string[];
}

function parseMarkdownBlocks(rawText: string): MarkdownBlock[] {
  const lines = rawText.split(/\r?\n/);
  const blocks: MarkdownBlock[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();

    // 1. Empty line
    if (!trimmed) {
      i++;
      continue;
    }

    // 2. Code Block
    if (trimmed.startsWith('```')) {
      const lang = trimmed.slice(3).trim();
      const codeLines: string[] = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith('```')) {
        codeLines.push(lines[i]);
        i++;
      }
      if (i < lines.length) i++; // skip closing ```
      blocks.push({ type: 'codeblock', lang, code: codeLines.join('\n') });
      continue;
    }

    // 3. Headings (#, ##, ###)
    const headingMatch = line.match(/^(#{1,6})\s+(.+)$/);
    if (headingMatch) {
      const level = headingMatch[1].length;
      blocks.push({ type: 'heading', level, content: headingMatch[2] });
      i++;
      continue;
    }

    // 4. Blockquotes (> ...)
    if (line.startsWith('>')) {
      const quoteLines: string[] = [];
      while (i < lines.length && lines[i].startsWith('>')) {
        quoteLines.push(lines[i].replace(/^>\s?/, ''));
        i++;
      }
      blocks.push({ type: 'blockquote', content: quoteLines.join('\n') });
      continue;
    }

    // 5. Horizontal rule (---, ***, ___)
    if (/^(?:-{3,}|\*{3,}|_{3,})$/.test(trimmed)) {
      blocks.push({ type: 'hr' });
      i++;
      continue;
    }

    // 6. Ordered list (1. item, 2. item)
    const olMatch = line.match(/^(\s*)(\d+)\.\s+(.+)$/);
    if (olMatch) {
      const items: string[] = [];
      while (i < lines.length) {
        const itemMatch = lines[i].match(/^(\s*)(\d+)\.\s+(.+)$/);
        if (itemMatch) {
          items.push(itemMatch[3]);
          i++;
        } else if (lines[i].trim() && lines[i].startsWith('   ') && items.length > 0) {
          items[items.length - 1] += '\n' + lines[i].trim();
          i++;
        } else {
          break;
        }
      }
      blocks.push({ type: 'ol', items });
      continue;
    }

    // 7. Unordered list (- item, * item, • item)
    const ulMatch = line.match(/^(\s*)(?:[-*•]|\+)\s+(.+)$/);
    if (ulMatch) {
      const items: string[] = [];
      while (i < lines.length) {
        const itemMatch = lines[i].match(/^(\s*)(?:[-*•]|\+)\s+(.+)$/);
        if (itemMatch) {
          items.push(itemMatch[2]);
          i++;
        } else if (lines[i].trim() && lines[i].startsWith('  ') && items.length > 0) {
          items[items.length - 1] += '\n' + lines[i].trim();
          i++;
        } else {
          break;
        }
      }
      blocks.push({ type: 'ul', items });
      continue;
    }

    // 8. Normal paragraph lines
    const pLines: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !lines[i].trim().startsWith('```') &&
      !lines[i].match(/^(#{1,6})\s+/) &&
      !lines[i].startsWith('>') &&
      !/^(?:-{3,}|\*{3,}|_{3,})$/.test(lines[i].trim()) &&
      !lines[i].match(/^(\s*)\d+\.\s+/) &&
      !lines[i].match(/^(\s*)(?:[-*•]|\+)\s+/)
    ) {
      pLines.push(lines[i]);
      i++;
    }
    if (pLines.length > 0) {
      blocks.push({ type: 'paragraph', content: pLines.join('\n') });
    }
  }

  return blocks;
}

function renderInline(
  text: string,
  citMap: Map<number, Citation>,
  onSelectCitation?: (cit: Citation) => void,
  keyPrefix = 'inl'
): JSX.Element[] {
  // Regex matches:
  // 1. inline code: `code`
  // 2. bold: **bold** or __bold__
  // 3. markdown link: [title](url)
  // 4. citation badge: [1]
  // 5. italic: *italic* or _italic_
  const tokenRegex = /(`[^`]+`|\*\*(?:[^*]|\*(?!\*))+\*\*|__(?:[^_]|_(?!_))+__|\[\d+\]|\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)|\*(?:[^*])+\*|_(?:[^_])+_)/g;

  const elements: JSX.Element[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  let count = 0;

  while ((match = tokenRegex.exec(text)) !== null) {
    if (match.index > lastIndex) {
      elements.push(<span key={`${keyPrefix}-t-${count++}`}>{text.slice(lastIndex, match.index)}</span>);
    }

    const token = match[0];
    if (token.startsWith('`') && token.endsWith('`')) {
      elements.push(
        <code class="msg-inline-code" key={`${keyPrefix}-c-${count++}`}>
          {token.slice(1, -1)}
        </code>
      );
    } else if (
      (token.startsWith('**') && token.endsWith('**')) ||
      (token.startsWith('__') && token.endsWith('__'))
    ) {
      const inner = token.slice(2, -2);
      elements.push(
        <strong key={`${keyPrefix}-b-${count++}`}>
          {renderInline(inner, citMap, onSelectCitation, `${keyPrefix}-b`)}
        </strong>
      );
    } else if (match[2] && match[3]) {
      // Link [title](url)
      elements.push(
        <a
          href={match[3]}
          class="msg-link"
          target="_blank"
          rel="noopener noreferrer"
          key={`${keyPrefix}-a-${count++}`}
        >
          {match[2]}
        </a>
      );
    } else if (/^\[\d+\]$/.test(token)) {
      const citIdx = parseInt(token.slice(1, -1), 10);
      const citation = citMap.get(citIdx);
      if (citation && onSelectCitation) {
        elements.push(
          <CitationBadge
            key={`${keyPrefix}-cit-${count++}`}
            citation={citation}
            onSelect={onSelectCitation}
          />
        );
      } else {
        elements.push(
          <span class="citation-bracket" key={`${keyPrefix}-cit-${count++}`}>
            [{citIdx}]
          </span>
        );
      }
    } else if (
      (token.startsWith('*') && token.endsWith('*')) ||
      (token.startsWith('_') && token.endsWith('_'))
    ) {
      const inner = token.slice(1, -1);
      elements.push(
        <em key={`${keyPrefix}-i-${count++}`}>
          {renderInline(inner, citMap, onSelectCitation, `${keyPrefix}-i`)}
        </em>
      );
    } else {
      elements.push(<span key={`${keyPrefix}-t-${count++}`}>{token}</span>);
    }

    lastIndex = tokenRegex.lastIndex;
  }

  if (lastIndex < text.length) {
    elements.push(<span key={`${keyPrefix}-t-${count++}`}>{text.slice(lastIndex)}</span>);
  }

  return elements;
}

function renderParagraphLines(
  paragraphText: string,
  citMap: Map<number, Citation>,
  onSelectCitation?: (cit: Citation) => void,
  keyPrefix = 'p'
): JSX.Element[] {
  const lines = paragraphText.split('\n');
  return lines.map((line, lIdx) => (
    <span key={`${keyPrefix}-l-${lIdx}`}>
      {renderInline(line, citMap, onSelectCitation, `${keyPrefix}-l-${lIdx}`)}
      {lIdx < lines.length - 1 && <br />}
    </span>
  ));
}

export function MarkdownRenderer({
  content,
  citations = [],
  onSelectCitation,
}: MarkdownRendererProps): JSX.Element {
  const citMap = new Map<number, Citation>();
  for (const c of citations) {
    citMap.set(c.index, c);
  }

  const blocks = parseMarkdownBlocks(content || '');

  return (
    <div class="message-content">
      {blocks.map((block, bIdx) => {
        switch (block.type) {
          case 'codeblock':
            return (
              <pre class="msg-code-block" key={bIdx}>
                <code>{block.code}</code>
              </pre>
            );

          case 'heading': {
            const hLevel = Math.min(Math.max(block.level || 3, 2), 5);
            if (hLevel === 2) {
              return (
                <h3 class="msg-h2" key={bIdx}>
                  {renderInline(block.content || '', citMap, onSelectCitation, `h-${bIdx}`)}
                </h3>
              );
            }
            if (hLevel === 3) {
              return (
                <h4 class="msg-h3" key={bIdx}>
                  {renderInline(block.content || '', citMap, onSelectCitation, `h-${bIdx}`)}
                </h4>
              );
            }
            return (
              <h5 class="msg-h4" key={bIdx}>
                {renderInline(block.content || '', citMap, onSelectCitation, `h-${bIdx}`)}
              </h5>
            );
          }

          case 'blockquote':
            return (
              <blockquote class="msg-blockquote" key={bIdx}>
                {renderParagraphLines(block.content || '', citMap, onSelectCitation, `bq-${bIdx}`)}
              </blockquote>
            );

          case 'hr':
            return <hr class="msg-hr" key={bIdx} />;

          case 'ol':
            return (
              <ol class="msg-ol" key={bIdx}>
                {(block.items || []).map((item, iIdx) => (
                  <li class="msg-li" key={iIdx}>
                    {renderInline(item, citMap, onSelectCitation, `ol-${bIdx}-${iIdx}`)}
                  </li>
                ))}
              </ol>
            );

          case 'ul':
            return (
              <ul class="msg-ul" key={bIdx}>
                {(block.items || []).map((item, iIdx) => (
                  <li class="msg-li" key={iIdx}>
                    {renderInline(item, citMap, onSelectCitation, `ul-${bIdx}-${iIdx}`)}
                  </li>
                ))}
              </ul>
            );

          case 'paragraph':
          default:
            return (
              <p class="msg-p" key={bIdx}>
                {renderParagraphLines(block.content || '', citMap, onSelectCitation, `p-${bIdx}`)}
              </p>
            );
        }
      })}
    </div>
  );
}
