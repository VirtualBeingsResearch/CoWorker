export type EditableComboboxOption = {
  value: string;
  label?: string;
  detail?: string;
};

export function filterEditableComboboxOptions(
  options: EditableComboboxOption[],
  query: string | null,
): EditableComboboxOption[] {
  if (query === null || query.trim() === '') return options;
  const normalized = query.trim().toLocaleLowerCase();
  return options.filter(option => [option.value, option.label, option.detail]
    .some(part => part?.toLocaleLowerCase().includes(normalized)));
}
