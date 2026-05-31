export function id(prefix: string) {
  return `${prefix}_${crypto.randomUUID().replaceAll("-", "")}`;
}

export function now() {
  return new Date();
}

export function iso(value: Date | string | null | undefined) {
  return value ? new Date(value).toISOString() : null;
}
