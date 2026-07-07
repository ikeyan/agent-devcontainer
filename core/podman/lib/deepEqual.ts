// /util/deepEqual.ts の vendored copy (kit の self-containment のため。原本と独立に保守する)。
import type { Primitive } from "type-fest";
import { isReadonlyArray, isReadonlyMap, isReadonlySet } from "./guard.ts";

// 循環参照は非対応
// ArrayBufferView (DataView, UInt8Arrayなど) は非対応
// 他のrealmのRegExp, Date, Map, Setは非対応
// オブジェクトはown propertyのみ比較
export type FastDeepEquatable =
  | Primitive
  | RegExp
  | Date
  | readonly FastDeepEquatable[]
  | FastDeepEquatable[]
  | FastDeepEquatableObject
  | ReadonlyMap<FastDeepEquatable, FastDeepEquatable>
  | ReadonlySet<FastDeepEquatable>;
export type FastDeepEquatableObject = { readonly [K in string | number]?: FastDeepEquatable };

type Compare<T> = (a: T, b: T) => boolean;

function equalWithPlainObjectCompare(
  compareObject: (innerEqual: Compare<FastDeepEquatable>) => Compare<FastDeepEquatableObject>,
) {
  const equal = (a: FastDeepEquatable, b: FastDeepEquatable) => {
    if (a === b) return true;

    if (a && b && typeof a === "object" && typeof b === "object") {
      if (isReadonlyArray(a)) {
        if (!isReadonlyArray(b)) return false;
        const length = a.length;
        if (length !== b.length) return false;
        for (let i = 0; i < length; ++i) {
          if (!equal(a[i], b[i])) return false;
        }
        return true;
      }
      if (isReadonlyArray(b)) return false;

      if (isReadonlyMap(a)) {
        if (!isReadonlyMap(b)) return false;
        if (a.size !== b.size) return false;
        for (const key of a.keys()) {
          if (!b.has(key)) return false;
        }
        for (const [k, v] of a.entries()) {
          if (!equal(v, b.get(k))) return false;
        }
        return true;
      }
      if (isReadonlyMap(b)) return false;

      if (isReadonlySet(a)) {
        if (!isReadonlySet(b)) return false;
        if (a.size !== b.size) return false;
        for (const i of a.entries()) if (!b.has(i[0])) return false;
        return true;
      }
      if (isReadonlySet(b)) return false;

      if (a instanceof RegExp) {
        if (!(b instanceof RegExp)) return false;
        return a.source === b.source && a.flags === b.flags;
      }
      if (b instanceof RegExp) return false;

      if (a instanceof Date) {
        if (!(b instanceof Date)) return false;
        return a.valueOf() === b.valueOf();
      }
      if (b instanceof Date) return false;

      return compareObject2(a, b);
    }

    return Number.isNaN(a) && Number.isNaN(b);
  };
  const compareObject2 = compareObject(equal);
  return equal;
}

export const deepEqual: <T extends FastDeepEquatable>(a: T, b: T) => boolean = equalWithPlainObjectCompare(
  (equal) => (a, b) => {
    const keys = Object.keys(a);
    const length = keys.length;
    if (length !== Object.keys(b).length) return false;

    for (let i = length; i-- !== 0; ) {
      if (!Object.hasOwn(b, keys[i])) return false;
    }

    for (let i = length; i-- !== 0; ) {
      const key = keys[i];
      if (!equal(a[key], b[key])) return false;
    }

    return true;
  },
);

export const deepEqualIgnoringUndefinedProperties: <T extends FastDeepEquatable>(a: T, b: T) => boolean =
  equalWithPlainObjectCompare((equal) => (a, b) => {
    const keys: string[] = [];
    for (const key of Object.keys(a)) {
      if (a[key] === undefined) continue;
      keys.push(key);
      if (!Object.hasOwn(b, key) || b[key] === undefined) return false;
    }
    const length = keys.length;
    if (length !== Object.keys(b).reduce((acc, key) => (b[key] !== undefined ? acc + 1 : acc), 0)) return false;

    for (let i = length; i-- !== 0; ) {
      const key = keys[i];
      if (!equal(a[key], b[key])) return false;
    }

    return true;
  });
