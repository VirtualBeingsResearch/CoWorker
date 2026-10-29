import { ChevronDown, ChevronUp, GripVertical } from 'lucide-react';
import { useRef, useState } from 'react';

import { t } from '../../i18n/admin';
import type { Json } from './types';

/**
 * Canonical backend order used by the admin surfaces. The order also matches
 * the server's default priority list, so the UI never invents a backend that
 * the configuration layer cannot persist.
 */
export const SEARCH_BACKENDS: { id: string; label: string }[] = [
  { id: 'bocha', label: 'Bocha 博查' },
  { id: 'qianfan', label: 'Qianfan 千帆' },
  { id: 'zhipu', label: 'Zhipu 智谱' },
  { id: 'zhipu_prime', label: 'Zhipu 套餐搜索' },
  { id: 'deepseek', label: 'DeepSeek' },
  { id: 'linkai', label: 'LinkAI' },
  { id: 'anysearch', label: 'AnySearch' },
  { id: 'serply', label: 'Serply' },
  { id: 'tavily', label: 'Tavily' },
  { id: 'searxng', label: 'SearXNG' },
  { id: 'keenable', label: 'Keenable' },
  { id: 'ddgs', label: 'DDGS' },
];

export function searchBackendOrder(value: unknown): string[] {
  const known = SEARCH_BACKENDS.map(backend => backend.id);
  const raw = Array.isArray(value) ? value.map(String) : [];
  const ordered = raw.filter((name, index) => known.includes(name) && raw.indexOf(name) === index);
  return [...ordered, ...known.filter(name => !ordered.includes(name))];
}

export function SearchBackendOrderEditor({ value, onChange, heading = true }: {
  value: Json;
  onChange: (key: string, next: unknown) => void;
  heading?: boolean;
}) {
  const order = searchBackendOrder(value.provider_order);
  const disabled = new Set(Array.isArray(value.disabled_providers) ? value.disabled_providers.map(String) : []);
  const labels = Object.fromEntries(SEARCH_BACKENDS.map(backend => [backend.id, backend.label]));
  const dragIndex = useRef<number | null>(null);
  const [dragging, setDragging] = useState<string | null>(null);
  const [overIndex, setOverIndex] = useState<number | null>(null);
  const reorder = (from: number, to: number) => {
    if (from === to || from < 0 || to < 0 || to >= order.length) return;
    const next = order.slice();
    const [item] = next.splice(from, 1);
    next.splice(to, 0, item);
    onChange('provider_order', next);
  };
  const toggle = (name: string, enabled: boolean) => {
    onChange('disabled_providers', order.filter(item => (item === name ? !enabled : disabled.has(item))));
  };
  const finishDrag = () => {
    dragIndex.current = null;
    setDragging(null);
    setOverIndex(null);
  };
  return <div className="field search-backend-order">
    {heading && <span>{t('自动顺序与启用')}</span>}
    <div className="search-backend-list" role="list">
      {order.map((name, index) => {
        const label = t(labels[name] || name);
        const className = [
          'search-backend-row',
          disabled.has(name) ? 'disabled' : '',
          dragging === name ? 'dragging' : '',
          overIndex === index && dragging && dragging !== name ? 'drop-target' : '',
        ].filter(Boolean).join(' ');
        return <div
          className={className}
          key={name}
          role="listitem"
          title={t('拖动调整 {{name}}', { name: label })}
          draggable
          onDragStart={event => {
            const target = event.target as HTMLElement;
            if (target.closest('input, button')) {
              event.preventDefault();
              return;
            }
            dragIndex.current = index;
            event.dataTransfer.effectAllowed = 'move';
            event.dataTransfer.setData('text/plain', name);
            if (event.currentTarget instanceof HTMLElement) event.dataTransfer.setDragImage(event.currentTarget, 28, 20);
            setDragging(name);
          }}
          onDragEnd={finishDrag}
          onDragOver={event => {
            event.preventDefault();
            event.dataTransfer.dropEffect = 'move';
            if (overIndex !== index) setOverIndex(index);
          }}
          onDrop={event => {
            event.preventDefault();
            const from = dragIndex.current;
            if (from !== null) reorder(from, index);
            finishDrag();
          }}
        >
          <span className="search-backend-grip" aria-hidden="true"><GripVertical size={16} /></span>
          <label className="switch">
            <input type="checkbox" checked={!disabled.has(name)} onChange={event => toggle(name, event.target.checked)} />
            <i />
            <span>{label}</span>
          </label>
          <div className="search-backend-move">
            <button type="button" className="ghost mini" disabled={index === 0} aria-label={t('上移 {{name}}', { name: label })} onClick={() => reorder(index, index - 1)}><ChevronUp size={14} /></button>
            <button type="button" className="ghost mini" disabled={index === order.length - 1} aria-label={t('下移 {{name}}', { name: label })} onClick={() => reorder(index, index + 1)}><ChevronDown size={14} /></button>
          </div>
        </div>;
      })}
    </div>
    <small>{t('拖动一行即可调整顺序。靠前的已启用且已配置后端会先被自动模式使用。')}</small>
  </div>;
}
