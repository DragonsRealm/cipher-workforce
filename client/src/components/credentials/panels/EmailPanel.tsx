/* eslint-disable react-hooks/exhaustive-deps -- form bootstrap effect runs once on mount; deps intentionally curated. */
/* eslint-disable react-hooks/incompatible-library -- react-hook-form's watch()
   returns a value the compiler cannot memoize safely. babel-plugin-react-compiler
   >= 1.0.0 knows this and skips memoizing the component, so the warning is
   advisory. That skip list did NOT exist before 1.0.0 (the repo sat on
   19.1.0-rc.3, where this suppression hid a real stale-render risk) — so do not
   downgrade the compiler, and note semver sorts rc.3 ABOVE 1.0.0. */
/**
 * EmailPanel — Himalaya IMAP/SMTP credentials.
 *
 * shadcn Form composition (react-hook-form + zod). Provider preset
 * (Select), email/password inputs, and a conditional custom IMAP/SMTP
 * block driven by `watch('provider')`. Each save persists fields via
 * saveApiKey under stable keys.
 */

import React, { useEffect, useMemo, useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { Eye, EyeOff, Loader2 } from 'lucide-react';

import { Input } from '@/components/ui/input';
import { ActionButton } from '@/components/ui/action-button';
import { Alert, AlertDescription } from '@/components/ui/alert';
import {
  Form,
  FormControl,
  FormDescription,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from '@/components/ui/form';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { useApiKeys } from '../../../hooks/useApiKeys';
import { NodeIcon } from '../../../assets/icons';
import { StatusCard } from '../primitives';
import {
  AUTH_NOTES,
  PROVIDER_OPTIONS,
  createEmailFormSchema,
  type EmailFormValues,
} from './schemas/email';
import type { ProviderConfig } from '../types';

const DEFAULT_VALUES: EmailFormValues = {
  provider: 'gmail',
  address: '',
  password: '',
  imapHost: '',
  imapPort: 993,
  smtpHost: '',
  smtpPort: 465,
};

const EmailPanel: React.FC<{ config: ProviderConfig; visible: boolean }> = ({ config, visible }) => {
  const { saveApiKey, getStoredApiKey, hasStoredKey, removeApiKey, isConnected } = useApiKeys();

  const [stored, setStored] = useState(false);
  const [address, setAddress] = useState('');
  const [loading, setLoading] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [revealPassword, setRevealPassword] = useState(false);

  // Schema is dependent on `stored` (password becomes optional once a key
  // is on file). Recreate the resolver when that flips.
  const schema = useMemo(() => createEmailFormSchema(!stored), [stored]);

  const form = useForm({
    resolver: zodResolver(schema),
    defaultValues: DEFAULT_VALUES,
    mode: 'onSubmit',
  });
  const provider = form.watch('provider');

  // Load stored credentials when the panel becomes visible.
  useEffect(() => {
    if (!visible || !isConnected) return;
    let cancelled = false;
    (async () => {
      try {
        const [providerKey, addr, hasPassword, imapHost, imapPort, smtpHost, smtpPort] = await Promise.all([
          getStoredApiKey('email_provider'),
          getStoredApiKey('email_address'),
          hasStoredKey('email_password'),
          getStoredApiKey('email_imap_host'),
          getStoredApiKey('email_imap_port'),
          getStoredApiKey('email_smtp_host'),
          getStoredApiKey('email_smtp_port'),
        ]);
        if (cancelled) return;
        form.reset({
          provider: providerKey || 'gmail',
          address: addr || '',
          password: '',
          imapHost: imapHost || '',
          imapPort: imapPort ? parseInt(imapPort, 10) : 993,
          smtpHost: smtpHost || '',
          smtpPort: smtpPort ? parseInt(smtpPort, 10) : 465,
        });
        setAddress(addr || '');
        setStored(hasPassword);
      } catch {
        if (!cancelled) setStored(false);
      }
    })();
    return () => { cancelled = true; };
  }, [visible, isConnected]);

  const onSubmit = async (values: EmailFormValues) => {
    setLoading('save');
    setError(null);
    try {
      await saveApiKey('email_provider', values.provider);
      await saveApiKey('email_address', values.address.trim());
      if (values.password?.trim()) await saveApiKey('email_password', values.password.trim());
      if (values.provider === 'custom') {
        if (values.imapHost) await saveApiKey('email_imap_host', values.imapHost.trim());
        if (values.imapPort != null) await saveApiKey('email_imap_port', String(values.imapPort));
        if (values.smtpHost) await saveApiKey('email_smtp_host', values.smtpHost.trim());
        if (values.smtpPort != null) await saveApiKey('email_smtp_port', String(values.smtpPort));
      }
      setStored(true);
      setAddress(values.address.trim());
      form.setValue('password', '');
    } catch (err: any) {
      setError(err.message || 'Failed to save email credentials');
    } finally {
      setLoading(null);
    }
  };

  const handleRemove = async () => {
    setLoading('remove');
    setError(null);
    try {
      await Promise.all([
        removeApiKey('email_password'),
        removeApiKey('email_address'),
        removeApiKey('email_provider'),
        removeApiKey('email_imap_host'),
        removeApiKey('email_imap_port'),
        removeApiKey('email_smtp_host'),
        removeApiKey('email_smtp_port'),
      ]);
      setStored(false);
      setAddress('');
      form.reset(DEFAULT_VALUES);
    } catch (err: any) {
      setError(err.message || 'Failed to remove credentials');
    } finally {
      setLoading(null);
    }
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4 p-5">
      <StatusCard
        icon={<NodeIcon icon={config.iconRef} className="h-6 w-6 text-2xl" />}
        title={config.name}
        status={{ stored, address }}
        rows={[
          { label: 'Status', ok: (s) => s.stored, trueText: 'Configured', falseText: 'Not configured' },
          ...(stored && address
            ? [{ label: 'Account', ok: () => true, trueText: address, falseText: '' }]
            : []),
        ]}
      />

      <Form {...form}>
        <form id="email-form" onSubmit={form.handleSubmit(onSubmit)} className="flex flex-col gap-4">
          <FormField
            control={form.control}
            name="provider"
            render={({ field }) => (
              <FormItem>
                <FormLabel>Provider</FormLabel>
                <Select value={field.value} onValueChange={field.onChange}>
                  <FormControl>
                    <SelectTrigger>
                      <SelectValue placeholder="Choose a provider" />
                    </SelectTrigger>
                  </FormControl>
                  <SelectContent>
                    {PROVIDER_OPTIONS.map((opt) => (
                      <SelectItem key={opt.value} value={opt.value}>
                        {opt.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <FormMessage />
              </FormItem>
            )}
          />

          <FormField
            control={form.control}
            name="address"
            render={({ field }) => (
              <FormItem>
                <FormLabel>Email Address</FormLabel>
                <FormControl>
                  <Input placeholder="you@example.com" {...field} />
                </FormControl>
                <FormMessage />
              </FormItem>
            )}
          />

          <FormField
            control={form.control}
            name="password"
            render={({ field }) => (
              <FormItem>
                <FormLabel className="flex items-center gap-2">
                  Password
                  {stored && (
                    <span className="text-xs font-normal text-muted-foreground">
                      (leave blank to keep existing)
                    </span>
                  )}
                </FormLabel>
                <FormControl>
                  <div className="relative">
                    <Input
                      type={revealPassword ? 'text' : 'password'}
                      placeholder={stored ? '••••••••' : 'App password or account password'}
                      className="font-mono pr-9"
                      {...field}
                    />
                    <button
                      type="button"
                      onClick={() => setRevealPassword((v) => !v)}
                      className="absolute top-1/2 right-2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                      aria-label={revealPassword ? 'Hide password' : 'Show password'}
                    >
                      {revealPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                    </button>
                  </div>
                </FormControl>
                <FormDescription>{AUTH_NOTES[provider]}</FormDescription>
                <FormMessage />
              </FormItem>
            )}
          />

          {provider === 'custom' && (
            <div className="rounded-md border border-border bg-muted p-3">
              <div className="mb-3 text-sm font-medium">Custom IMAP / SMTP</div>
              <div className="flex gap-3">
                <FormField
                  control={form.control}
                  name="imapHost"
                  render={({ field }) => (
                    <FormItem className="flex-[2]">
                      <FormLabel>IMAP Host</FormLabel>
                      <FormControl>
                        <Input placeholder="imap.example.com" {...field} />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <FormField
                  control={form.control}
                  name="imapPort"
                  render={({ field }) => (
                    <FormItem className="flex-1">
                      <FormLabel>IMAP Port</FormLabel>
                      <FormControl>
                        <Input
                          type="number"
                          min={1}
                          max={65535}
                          {...field}
                          value={field.value ?? ''}
                          onChange={(e) => field.onChange(e.target.value === '' ? undefined : Number(e.target.value))}
                        />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
              </div>
              <div className="mt-3 flex gap-3">
                <FormField
                  control={form.control}
                  name="smtpHost"
                  render={({ field }) => (
                    <FormItem className="flex-[2]">
                      <FormLabel>SMTP Host</FormLabel>
                      <FormControl>
                        <Input placeholder="smtp.example.com" {...field} />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <FormField
                  control={form.control}
                  name="smtpPort"
                  render={({ field }) => (
                    <FormItem className="flex-1">
                      <FormLabel>SMTP Port</FormLabel>
                      <FormControl>
                        <Input
                          type="number"
                          min={1}
                          max={65535}
                          {...field}
                          value={field.value ?? ''}
                          onChange={(e) => field.onChange(e.target.value === '' ? undefined : Number(e.target.value))}
                        />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
              </div>
            </div>
          )}
        </form>
      </Form>

      {error && (
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      <div className="flex-1" />

      <div className="flex justify-center gap-2 border-t border-border pt-3">
        <ActionButton
          intent="save"
          type="submit"
          form="email-form"
          disabled={loading === 'save'}
        >
          {loading === 'save' && <Loader2 className="h-4 w-4 animate-spin" />}
          Save
        </ActionButton>
        {stored && (
          <ActionButton
            intent="stop"
            type="button"
            onClick={handleRemove}
            disabled={loading === 'remove'}
          >
            {loading === 'remove' && <Loader2 className="h-4 w-4 animate-spin" />}
            Remove
          </ActionButton>
        )}
      </div>
    </div>
  );
};

export default EmailPanel;
