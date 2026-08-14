import { useEffect, useMemo, useRef, useState } from 'react';
import { Check, ChevronDown } from 'lucide-react';
import {
  filterEditableComboboxOptions,
  type EditableComboboxOption,
} from './comboboxOptions';

type EditableComboboxProps = {
  id: string;
  value: string;
  options: EditableComboboxOption[];
  onChange: (value: string) => void;
  placeholder?: string;
  emptyMessage: string;
  toggleLabel: string;
  maxLength?: number;
  normalize?: (value: string) => string;
};

export function EditableCombobox({
  id,
  value,
  options,
  onChange,
  placeholder,
  emptyMessage,
  toggleLabel,
  maxLength,
  normalize = next => next,
}: EditableComboboxProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState<string | null>(null);
  const [highlightedIndex, setHighlightedIndex] = useState(-1);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const optionRefs = useRef<Array<HTMLLIElement | null>>([]);
  const listboxId = `${id}-options`;
  const visibleOptions = useMemo(
    () => filterEditableComboboxOptions(options, query),
    [options, query],
  );
  const highlightedId = highlightedIndex >= 0
    ? `${id}-option-${highlightedIndex}`
    : undefined;

  const closeMenu = () => {
    setOpen(false);
    setQuery(null);
    setHighlightedIndex(-1);
  };
  const openAll = () => {
    const selectedIndex = options.findIndex(option => option.value === value);
    setQuery(null);
    setHighlightedIndex(selectedIndex);
    setOpen(true);
  };
  const choose = (next: string) => {
    onChange(next);
    closeMenu();
    window.requestAnimationFrame(() => inputRef.current?.focus());
  };
  const moveHighlight = (direction: 1 | -1) => {
    if (!visibleOptions.length) {
      setHighlightedIndex(-1);
      return;
    }
    setHighlightedIndex(current => current < 0
      ? (direction > 0 ? 0 : visibleOptions.length - 1)
      : (current + direction + visibleOptions.length) % visibleOptions.length);
  };

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      if (event.target instanceof Node && !rootRef.current?.contains(event.target)) closeMenu();
    };
    document.addEventListener('pointerdown', onPointerDown);
    return () => document.removeEventListener('pointerdown', onPointerDown);
  }, [open]);

  useEffect(() => {
    if (highlightedIndex >= 0) optionRefs.current[highlightedIndex]?.scrollIntoView({ block: 'nearest' });
  }, [highlightedIndex, visibleOptions]);

  return <div className={`editable-combobox${open ? ' open' : ''}`} ref={rootRef}>
    <input
      id={id}
      ref={inputRef}
      role="combobox"
      aria-autocomplete="list"
      aria-haspopup="listbox"
      aria-expanded={open}
      aria-controls={listboxId}
      aria-activedescendant={highlightedId}
      autoComplete="off"
      spellCheck={false}
      maxLength={maxLength}
      value={value}
      placeholder={placeholder}
      onFocus={() => { if (!open) openAll(); }}
      onClick={() => { if (!open) openAll(); }}
      onChange={event => {
        const next = normalize(event.target.value);
        onChange(next);
        setQuery(next);
        setHighlightedIndex(-1);
        setOpen(true);
      }}
      onKeyDown={event => {
        if (event.key === 'ArrowDown') {
          event.preventDefault();
          if (!open) openAll(); else moveHighlight(1);
        } else if (event.key === 'ArrowUp') {
          event.preventDefault();
          if (!open) openAll(); else moveHighlight(-1);
        } else if (event.key === 'Enter' && open) {
          if (highlightedIndex >= 0 && visibleOptions[highlightedIndex]) {
            event.preventDefault();
            choose(visibleOptions[highlightedIndex].value);
          } else {
            closeMenu();
          }
        } else if (event.key === 'Escape' && open) {
          event.preventDefault();
          closeMenu();
        } else if (event.key === 'Tab') {
          closeMenu();
        }
      }}
    />
    <button
      type="button"
      className="editable-combobox-toggle"
      aria-label={toggleLabel}
      aria-expanded={open}
      aria-controls={listboxId}
      onPointerDown={event => event.preventDefault()}
      onClick={() => {
        if (open) closeMenu(); else openAll();
        inputRef.current?.focus({ preventScroll: true });
      }}
    ><ChevronDown size={15} /></button>
    {open && <ul className="editable-combobox-listbox" id={listboxId} role="listbox">
      {visibleOptions.length ? visibleOptions.map((option, index) => <li
        id={`${id}-option-${index}`}
        ref={node => { optionRefs.current[index] = node; }}
        className={`${index === highlightedIndex ? 'active' : ''}${option.value === value ? ' selected' : ''}`}
        role="option"
        aria-selected={option.value === value}
        key={option.value}
        onMouseEnter={() => setHighlightedIndex(index)}
        onPointerDown={event => event.preventDefault()}
        onClick={() => choose(option.value)}
      >
        <span><b>{option.value}</b>{option.label && option.label !== option.value && <small>{option.label}</small>}</span>
        <span className="editable-combobox-option-end">{option.detail && <em>{option.detail}</em>}{option.value === value && <Check size={14} />}</span>
      </li>) : <li className="editable-combobox-empty" role="status">{emptyMessage}</li>}
    </ul>}
  </div>;
}
