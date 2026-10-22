// /util/guard.ts の vendored copy (deepEqual.ts の依存として self-containment のため複製。原本と独立に保守する)。
export const isReadonlyArray = (value: unknown): value is readonly unknown[] => Array.isArray(value);
export const isReadonlyMap = (value: unknown): value is ReadonlyMap<unknown, unknown> => value instanceof Map;
export const isReadonlySet = (value: unknown): value is ReadonlySet<unknown> => value instanceof Set;
