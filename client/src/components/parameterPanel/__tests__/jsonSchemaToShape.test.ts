/**
 * jsonSchemaToShape — JSON-Schema-7 flattening for the variable picker.
 *
 * The backend serves output schemas straight from Pydantic
 * (`model_json_schema()`), which expresses `Optional[X]` as an `anyOf` with
 * no top-level `type`, and a nested BaseModel as a `$ref` into `$defs`.
 * Every field on the trigger output models is Optional, so without
 * resolution the picker typed all of them 'any' and nested blocks
 * (telegramReceive.media, whatsappReceive.group_info) had no draggable
 * leaves.
 */
import { describe, it, expect } from 'vitest';

import { jsonSchemaToShape } from '../InputSection';

describe('jsonSchemaToShape', () => {
  it('returns null for a non-object or property-less schema', () => {
    expect(jsonSchemaToShape(null)).toBeNull();
    expect(jsonSchemaToShape(undefined)).toBeNull();
    expect(jsonSchemaToShape({})).toBeNull();
  });

  it('maps plain primitives, normalising integer to number', () => {
    const shape = jsonSchemaToShape({
      properties: {
        name: { type: 'string' },
        count: { type: 'integer' },
        ratio: { type: 'number' },
        ok: { type: 'boolean' },
        items: { type: 'array' },
      },
    });

    expect(shape).toEqual({
      name: 'string',
      count: 'number',
      ratio: 'number',
      ok: 'boolean',
      items: 'array',
    });
  });

  it('unwraps Optional[X] to the underlying primitive type', () => {
    // Exactly what Pydantic emits for `text: Optional[str] = None`.
    const shape = jsonSchemaToShape({
      properties: {
        text: { anyOf: [{ type: 'string' }, { type: 'null' }], default: null },
        message_id: { anyOf: [{ type: 'integer' }, { type: 'null' }], default: null },
      },
    });

    expect(shape).toEqual({ text: 'string', message_id: 'number' });
  });

  it('resolves a $ref into $defs so nested fields become draggable leaves', () => {
    // The telegramReceive.media shape.
    const shape = jsonSchemaToShape({
      properties: {
        media: {
          anyOf: [{ $ref: '#/$defs/TelegramMedia' }, { type: 'null' }],
          default: null,
        },
      },
      $defs: {
        TelegramMedia: {
          type: 'object',
          properties: {
            kind: { anyOf: [{ type: 'string' }, { type: 'null' }] },
            file_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
            file_size: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
          },
        },
      },
    });

    expect(shape).toEqual({
      media: { kind: 'string', file_id: 'string', file_size: 'number' },
    });
  });

  it('resolves a bare $ref without an Optional wrapper', () => {
    const shape = jsonSchemaToShape({
      properties: { group_info: { $ref: '#/$defs/GroupInfo' } },
      $defs: {
        GroupInfo: { type: 'object', properties: { subject: { type: 'string' } } },
      },
    });

    expect(shape).toEqual({ group_info: { subject: 'string' } });
  });

  it('keeps $defs reachable through nested recursion', () => {
    const shape = jsonSchemaToShape({
      properties: {
        outer: {
          type: 'object',
          properties: { inner: { $ref: '#/$defs/Leaf' } },
        },
      },
      $defs: { Leaf: { type: 'object', properties: { v: { type: 'string' } } } },
    });

    expect(shape).toEqual({ outer: { inner: { v: 'string' } } });
  });

  it('does not hang on a self-referencing $ref', () => {
    const shape = jsonSchemaToShape({
      properties: { node: { $ref: '#/$defs/Node' } },
      $defs: {
        Node: {
          type: 'object',
          properties: {
            label: { type: 'string' },
            child: { $ref: '#/$defs/Node' },
          },
        },
      },
    });

    expect(shape?.node).toMatchObject({ label: 'string' });
  });

  it('falls back to any for an unresolvable ref or an untyped property', () => {
    const shape = jsonSchemaToShape({
      properties: {
        missing: { $ref: '#/$defs/NotThere' },
        external: { $ref: 'https://example.com/schema.json' },
        untyped: { description: 'no type at all' },
      },
    });

    expect(shape).toEqual({ missing: 'any', external: 'any', untyped: 'any' });
  });

  it('picks the first non-null branch of a multi-type union', () => {
    const shape = jsonSchemaToShape({
      properties: {
        chat_id: { anyOf: [{ type: 'integer' }, { type: 'string' }, { type: 'null' }] },
      },
    });

    expect(shape).toEqual({ chat_id: 'number' });
  });
});
