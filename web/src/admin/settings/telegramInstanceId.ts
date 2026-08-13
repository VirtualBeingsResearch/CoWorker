const FIRST_CHARACTERS = 'abcdefghijklmnopqrstuvwxyz';
const REMAINING_CHARACTERS = `${FIRST_CHARACTERS}0123456789`;
const RANDOM_ATTEMPTS = 16;
const GENERATED_ID_LENGTH = 4;
const ID_SPACE_SIZE = FIRST_CHARACTERS.length * REMAINING_CHARACTERS.length ** 3;

export const TELEGRAM_INSTANCE_ID_PATTERN = /^[a-z][a-z0-9_-]{0,31}$/;

export function defaultTelegramDisplayName(instanceId: string): string {
  return `Telegram ${instanceId}`;
}

function randomCharacter(characters: string, random: () => number): string {
  return characters[Math.floor(random() * characters.length)];
}

function randomCandidate(random: () => number): string {
  return randomCharacter(FIRST_CHARACTERS, random)
    + Array.from(
      { length: GENERATED_ID_LENGTH - 1 },
      () => randomCharacter(REMAINING_CHARACTERS, random),
    ).join('');
}

function candidateAt(index: number): string {
  let remainder = index;
  const suffix = Array.from({ length: GENERATED_ID_LENGTH - 1 }, () => {
    const character = REMAINING_CHARACTERS[remainder % REMAINING_CHARACTERS.length];
    remainder = Math.floor(remainder / REMAINING_CHARACTERS.length);
    return character;
  }).reverse().join('');
  return `${FIRST_CHARACTERS[remainder]}${suffix}`;
}

export function generateTelegramInstanceId(
  existingIds: Iterable<string>,
  random: () => number = Math.random,
): string {
  const existing = new Set(existingIds);
  for (let attempt = 0; attempt < RANDOM_ATTEMPTS; attempt += 1) {
    const candidate = randomCandidate(random);
    if (!existing.has(candidate)) return candidate;
  }
  for (let index = 0; index < ID_SPACE_SIZE; index += 1) {
    const candidate = candidateAt(index);
    if (!existing.has(candidate)) return candidate;
  }
  throw new Error('Telegram instance ID space is exhausted');
}
