import { JSX } from 'preact';
import { useState } from 'preact/hooks';
import { Citation } from '../types';

interface CitationBadgeProps {
  citation: Citation;
  onSelect: (citation: Citation) => void;
}

export function CitationBadge({ citation, onSelect }: CitationBadgeProps): JSX.Element {
  const [showTooltip, setShowTooltip] = useState(false);

  const locatorDisplay = citation.page_number
    ? `p.${citation.page_number}`
    : citation.source_locator || 'p.1';

  return (
    <span class="citation-badge-wrapper">
      <button
        type="button"
        onClick={() => onSelect(citation)}
        onMouseEnter={() => setShowTooltip(true)}
        onMouseLeave={() => setShowTooltip(false)}
        onFocus={() => setShowTooltip(true)}
        onBlur={() => setShowTooltip(false)}
        aria-label={`View citation source / 檢視引用來源: ${citation.filename} ${locatorDisplay}`}
        class="citation-badge-btn"
        data-file-id={citation.file_id}
        data-citation-index={citation.index}
      >
        [{citation.index}]
      </button>

      {showTooltip && (
        <div role="tooltip" class="citation-tooltip">
          <div class="citation-tooltip-header">
            <span class="citation-tooltip-filename" title={citation.filename}>
              {citation.filename}
            </span>
            <span class="citation-tooltip-locator">{locatorDisplay}</span>
          </div>
          <p class="citation-tooltip-snippet">
            {citation.snippet || 'Click to view document snippet...'}
          </p>
          <div class="citation-tooltip-footer">
            Click to preview source ↗
          </div>
        </div>
      )}
    </span>
  );
}
