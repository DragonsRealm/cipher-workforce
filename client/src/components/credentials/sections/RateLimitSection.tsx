/* eslint-disable react-hooks/incompatible-library -- react-hook-form's watch()
   cannot be memoized safely; the compiler (>= 1.0.0) skips this component
   rather than miscompiling it, so the warning is advisory. Not true before
   1.0.0 — do not downgrade. */
/**
 * RateLimitSection — WhatsApp rate limit configuration.
 *
 * shadcn Form composition (react-hook-form + zod). The header Switch
 * toggles rate-limiting entirely; interior fields govern delays, limits
 * and behavior. `response_rate_threshold` is stored as a fraction but
 * displayed as a percentage — transformed in the Input onChange/value.
 */

import React, { useCallback, useEffect, useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { Loader2 } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Switch } from '@/components/ui/switch';
import { Alert, AlertDescription } from '@/components/ui/alert';
import {
  Form,
  FormControl,
  FormDescription,
  FormField,
  FormItem,
  FormLabel,
} from '@/components/ui/form';
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from '@/components/ui/accordion';
import { useWebSocket, type RateLimitConfig, type RateLimitStats } from '../../../contexts/WebSocketContext';

const formSchema = z.object({
  enabled: z.boolean().default(false),
  min_delay_ms: z.number().int().min(0).optional(),
  max_delay_ms: z.number().int().min(0).optional(),
  typing_delay_ms: z.number().int().min(0).optional(),
  link_extra_delay_ms: z.number().int().min(0).optional(),
  max_messages_per_minute: z.number().int().min(0).optional(),
  max_messages_per_hour: z.number().int().min(0).optional(),
  max_new_contacts_per_day: z.number().int().min(0).optional(),
  simulate_typing: z.boolean().optional(),
  randomize_delays: z.boolean().optional(),
  pause_on_low_response: z.boolean().optional(),
  response_rate_threshold: z.number().min(0).max(1).optional(),
});

const Stat: React.FC<{ label: string; value: React.ReactNode }> = ({ label, value }) => (
  <div className="flex flex-col">
    <span className="text-xs text-muted-foreground">{label}</span>
    <span className="text-lg font-semibold">{value}</span>
  </div>
);

const numberOnChange =
  (field: { onChange: (v: number | undefined) => void }) =>
  (e: React.ChangeEvent<HTMLInputElement>) => {
    const raw = e.target.value;
    field.onChange(raw === '' ? undefined : Number(raw));
  };

const RateLimitSection: React.FC = () => {
  const { getWhatsAppRateLimitConfig, setWhatsAppRateLimitConfig, unpauseWhatsAppRateLimit } = useWebSocket();
  const [stats, setStats] = useState<RateLimitStats | null>(null);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);

  const form = useForm({
    resolver: zodResolver(formSchema),
    defaultValues: { enabled: false },
  });
  const pauseOnLowResponse = form.watch('pause_on_low_response');
  const { isDirty } = form.formState;

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const result = await getWhatsAppRateLimitConfig();
      if (result.success && result.config) {
        form.reset(result.config as Partial<RateLimitConfig>);
        setStats(result.stats ?? null);
        setLoaded(true);
      }
    } finally {
      setLoading(false);
    }
  }, [form, getWhatsAppRateLimitConfig]);

  useEffect(() => { load(); }, [load]);

  const onSubmit = useCallback(
    async (values: z.infer<typeof formSchema>) => {
      setLoading(true);
      try {
        const result = await setWhatsAppRateLimitConfig(values as RateLimitConfig);
        if (result.success && result.config) {
          form.reset(result.config as Partial<RateLimitConfig>);
        }
      } finally {
        setLoading(false);
      }
    },
    [form, setWhatsAppRateLimitConfig],
  );

  const handleUnpause = useCallback(async () => {
    setLoading(true);
    try {
      const result = await unpauseWhatsAppRateLimit();
      if (result.success && result.stats) setStats(result.stats);
    } finally {
      setLoading(false);
    }
  }, [unpauseWhatsAppRateLimit]);

  return (
    <Accordion type="single" collapsible>
      <AccordionItem value="ratelimits">
        <AccordionTrigger>
          <div className="flex w-full items-center justify-between gap-2">
            <span>Rate Limits</span>
            <FormField
              control={form.control}
              name="enabled"
              render={({ field }) => (
                <span onClick={(e) => e.stopPropagation()}>
                  <Switch checked={!!field.value} onCheckedChange={field.onChange} />
                </span>
              )}
            />
          </div>
        </AccordionTrigger>
        <AccordionContent>
          {!loaded ? (
            <div className="flex justify-center p-4 text-sm text-muted-foreground">Loading...</div>
          ) : (
            <Form {...form}>
              <form onSubmit={form.handleSubmit(onSubmit)} className="flex flex-col gap-3">
                {stats && (
                  <div className="mb-3 flex flex-wrap gap-4">
                    <Stat label="Last Minute" value={stats.messages_sent_last_minute} />
                    <Stat label="Last Hour" value={stats.messages_sent_last_hour} />
                    <Stat label="Today" value={stats.messages_sent_today} />
                    <Stat label="New Contacts" value={stats.new_contacts_today} />
                    <Stat label="Responses" value={stats.responses_received} />
                    <Stat
                      label="Response Rate"
                      value={`${Math.round((stats.response_rate || 0) * 100)}%`}
                    />
                  </div>
                )}

                {stats?.is_paused && (
                  <Alert variant="warning">
                    <AlertDescription className="flex items-center justify-between gap-3">
                      <span>{stats.pause_reason || 'Paused'}</span>
                      <Button size="sm" variant="outline" type="button" onClick={handleUnpause}>
                        Unpause
                      </Button>
                    </AlertDescription>
                  </Alert>
                )}

                <div className="text-xs font-medium text-muted-foreground">
                  Message Delays (milliseconds)
                </div>
                <div className="flex flex-wrap gap-3">
                  {(
                    [
                      ['min_delay_ms', 'Min Delay'],
                      ['max_delay_ms', 'Max Delay'],
                      ['typing_delay_ms', 'Typing Duration'],
                      ['link_extra_delay_ms', 'Link Extra Delay'],
                    ] as const
                  ).map(([name, label]) => (
                    <FormField
                      key={name}
                      control={form.control}
                      name={name}
                      render={({ field }) => (
                        <FormItem>
                          <FormLabel className="text-xs">{label}</FormLabel>
                          <FormControl>
                            <Input
                              type="number"
                              min={0}
                              className="w-28"
                              value={field.value ?? ''}
                              onChange={numberOnChange(field)}
                            />
                          </FormControl>
                        </FormItem>
                      )}
                    />
                  ))}
                </div>

                <div className="text-xs font-medium text-muted-foreground">Message Limits</div>
                <div className="flex flex-wrap gap-3">
                  {(
                    [
                      ['max_messages_per_minute', 'Per Minute'],
                      ['max_messages_per_hour', 'Per Hour'],
                      ['max_new_contacts_per_day', 'New Contacts/Day'],
                    ] as const
                  ).map(([name, label]) => (
                    <FormField
                      key={name}
                      control={form.control}
                      name={name}
                      render={({ field }) => (
                        <FormItem>
                          <FormLabel className="text-xs">{label}</FormLabel>
                          <FormControl>
                            <Input
                              type="number"
                              min={0}
                              className="w-32"
                              value={field.value ?? ''}
                              onChange={numberOnChange(field)}
                            />
                          </FormControl>
                        </FormItem>
                      )}
                    />
                  ))}
                </div>

                <div className="text-xs font-medium text-muted-foreground">Behavior</div>
                <div className="flex w-full flex-col gap-2">
                  {(
                    [
                      ['simulate_typing', 'Simulate Typing', 'Show typing indicator before sending'],
                      ['randomize_delays', 'Randomize Delays', 'Add variance between min/max delay'],
                      [
                        'pause_on_low_response',
                        'Pause on Low Response',
                        'Auto-pause if response rate drops below threshold',
                      ],
                    ] as const
                  ).map(([name, label, description]) => (
                    <FormField
                      key={name}
                      control={form.control}
                      name={name}
                      render={({ field }) => (
                        <FormItem className="flex flex-row items-center justify-between rounded-md border border-border p-3">
                          <div className="space-y-0.5">
                            <FormLabel>{label}</FormLabel>
                            <FormDescription>{description}</FormDescription>
                          </div>
                          <FormControl>
                            <Switch checked={!!field.value} onCheckedChange={field.onChange} />
                          </FormControl>
                        </FormItem>
                      )}
                    />
                  ))}

                  {pauseOnLowResponse && (
                    <FormField
                      control={form.control}
                      name="response_rate_threshold"
                      render={({ field }) => (
                        <FormItem>
                          <FormLabel>Response Rate Threshold (%)</FormLabel>
                          <FormControl>
                            <Input
                              type="number"
                              min={0}
                              max={100}
                              value={Math.round((field.value ?? 0.3) * 100)}
                              onChange={(e) => {
                                const raw = e.target.value;
                                field.onChange(raw === '' ? undefined : Number(raw) / 100);
                              }}
                            />
                          </FormControl>
                        </FormItem>
                      )}
                    />
                  )}
                </div>

                <Button
                  type="submit"
                  disabled={!isDirty || loading}
                  variant={isDirty ? 'default' : 'outline'}
                  className="w-full"
                >
                  {loading && <Loader2 className="h-4 w-4 animate-spin" />}
                  Save Changes
                </Button>
              </form>
            </Form>
          )}
        </AccordionContent>
      </AccordionItem>
    </Accordion>
  );
};

export default RateLimitSection;
