// /util/guard.ts の vendored copy (deepEqual.ts の依存として self-containment のため複製。原本と独立に保守する)。
import type { IsEqual, IsLiteral, Primitive, SetOptional } from "type-fest";

export const isNonNull: <T>(value: T) => value is NonNullable<T> = (value) => value != null;
export const isNull: (value: unknown) => value is null = (value) => value === null;
export const isUndefined: (value: unknown) => value is undefined = (value) => value === undefined;
export const isNullOrUndefined: (value: unknown) => value is null | undefined = (value) => value == null;
export const isNotUndefined = (input: unknown): input is NonNullable<unknown> | null => input !== undefined;
export const isSomeObject: (value: unknown) => value is object = (value): value is object => Object(value) === value;
export const isStringKeyOf =
  <O extends Record<string, unknown>>(obj: O) =>
  <K>(key: K): key is K & string & keyof O =>
    typeof key === "string" && key in obj;
type UndefinedableKeys<T extends Record<string, unknown>> = {
  [K in keyof T]-?: undefined extends T[K] ? K : never;
}[keyof T];
(_: IsEqual<UndefinedableKeys<{ a: number | undefined; b: string }>, "a">): true => _;
(_: IsEqual<UndefinedableKeys<{ a: number | undefined; b?: string }>, "a" | "b">): true => _;
/** make undefinedable properties optional */
type AddQuestionMark<T extends Record<string, unknown>> = SetOptional<T, UndefinedableKeys<T>>;
(_: IsEqual<AddQuestionMark<{ a: number; b: string }>, { a: number; b: string }>): true => _;
(_: IsEqual<AddQuestionMark<{ a: number | undefined; b: string }>, { a?: number | undefined; b: string }>): true => _;
export const isObjectOf =
  <const Properties extends Record<string, unknown>>(
    propertiesGuard: {
      [K in keyof Properties]: unknown extends Properties[K]
        ? (value: unknown) => boolean
        : (value: unknown) => value is Properties[K];
    },
  ) =>
  (value: unknown): value is AddQuestionMark<Properties> => {
    if (!isSomeObject(value)) {
      return false;
    }
    for (const [key, guard] of Object.entries(propertiesGuard)) {
      if (!guard(key in value ? (value as Record<string, unknown>)[key] : undefined)) {
        return false;
      }
    }
    return true;
  };
export const isTupleOf =
  <const T extends (((value: unknown) => value is unknown) | ((value: unknown) => boolean))[]>(predicates: T) =>
  (
    value: unknown,
  ): value is {
    [K in keyof T]: T[K] extends (value: unknown) => value is infer R
      ? R
      : ((value: unknown) => boolean) extends T[K]
        ? unknown
        : never;
  } =>
    isReadonlyArray(value) &&
    value.length === predicates.length &&
    predicates.every((predicate, index) => predicate(value[index]));
export const isString: (value: unknown) => value is string = (value) => typeof value === "string";
export const isStringStartsWith =
  <const Prefix extends string>(prefix: Prefix) =>
  (value: unknown): value is `${Prefix}${string}` =>
    isString(value) && value.startsWith(prefix);
export const isNumber: (value: unknown) => value is number = (value) => typeof value === "number";
type EnsureLiteral<T extends Primitive> = IsLiteral<T> extends true ? T : never;
export const isLiteral =
  <T extends Primitive>(literal: EnsureLiteral<T>) =>
  (value: unknown): value is T =>
    value === literal;
export const isReadonlyArray = (value: unknown): value is readonly unknown[] => Array.isArray(value);
export const isReadonlyArrayOf =
  <T>(guard: (value: unknown) => value is T) =>
  (value: unknown): value is readonly T[] =>
    isReadonlyArray(value) && value.every((item) => guard(item));
export const isUndefinedableOf =
  <T>(guard: (value: unknown) => value is T) =>
  (value: unknown): value is T | undefined =>
    value === undefined || guard(value);
export const isReadonlyMap = (value: unknown): value is ReadonlyMap<unknown, unknown> => value instanceof Map;
export const isReadonlySet = (value: unknown): value is ReadonlySet<unknown> => value instanceof Set;
export const narrowGuard =
  <In, Out extends In>(guard: (value: In) => value is Out) =>
  <In2 extends In>(value: In2): value is Out & In2 =>
    guard(value);

export const isEmptyObject = (value: unknown): value is Record<string, never> =>
  isSomeObject(value) && !Array.isArray(value) && Object.keys(value).length === 0;
export function isNonEmptyArray<T>(value: T[]): value is [T, ...T[]];
export function isNonEmptyArray<T>(value: readonly T[]): value is readonly [T, ...T[]];
export function isNonEmptyArray<T>(value: readonly T[]): value is readonly [T, ...T[]] {
  return Array.isArray(value) && value.length > 0;
}
