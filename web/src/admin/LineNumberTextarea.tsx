import { forwardRef, type TextareaHTMLAttributes, type UIEvent, useRef } from 'react';

import { t } from '../i18n/admin';

type LineNumberTextareaProps = TextareaHTMLAttributes<HTMLTextAreaElement> & {
  wrapperClassName?: string;
  showSummary?: boolean;
};

export const LineNumberTextarea = forwardRef<HTMLTextAreaElement, LineNumberTextareaProps>(function LineNumberTextarea({
  className = '',
  onScroll,
  showSummary = true,
  value = '',
  wrapperClassName = '',
  wrap = 'off',
  ...props
}, forwardedRef) {
  const gutter = useRef<HTMLDivElement | null>(null);
  const text = String(value ?? '');
  const lines = text.split('\n');
  const lineCount = text ? lines.length : 0;
  const blankLines = text ? lines.filter(line => !line.trim()).length : 0;
  const setTextareaRef = (element: HTMLTextAreaElement | null) => {
    if (typeof forwardedRef === 'function') forwardedRef(element);
    else if (forwardedRef) forwardedRef.current = element;
  };
  const syncScroll = (event: UIEvent<HTMLTextAreaElement>) => {
    if (gutter.current) gutter.current.scrollTop = event.currentTarget.scrollTop;
    onScroll?.(event);
  };

  return <div className={`line-number-field${wrapperClassName ? ` ${wrapperClassName}` : ''}`}>
    <div className="line-number-editor">
      <div className="line-number-gutter" ref={gutter} aria-hidden="true">
        {lines.map((line, index) => <span className={!line.trim() ? 'blank' : ''} key={index}>{index + 1}</span>)}
      </div>
      <textarea {...props} className={className} ref={setTextareaRef} value={value} wrap={wrap} onScroll={syncScroll} />
    </div>
    {showSummary && <small className="line-number-summary">{t('{{lines}} 行 · {{blank}} 个空白行', { lines: lineCount, blank: blankLines })}</small>}
  </div>;
});
