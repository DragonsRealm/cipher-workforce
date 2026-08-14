import React from 'react';
import GoogleMapsPicker from './GoogleMapsPicker';
import { useAppTheme } from '../../hooks/useAppTheme';

interface MapsPreviewPanelProps {
  lat: number;
  lng: number;
  zoom?: number;
  mapTypeId?: string;
  apiKey?: string;
  onLocationClick: (lat: number, lng: number) => void;
  title?: string;
  description?: string;
}

const MapsPreviewPanel: React.FC<MapsPreviewPanelProps> = ({
  lat,
  lng,
  zoom = 13,
  mapTypeId = 'ROADMAP',
  apiKey,
  onLocationClick,
  title = 'Maps Preview',
  description = 'Interactive map based on current parameters'
}) => {
  const theme = useAppTheme();

  return (
    <div
      style={{
        width: '100%',
        height: '100%',
        display: 'flex',
        flexDirection: 'column',
        backgroundColor: theme.colors.backgroundPanel
      }}>
      {/* Maps Header */}
      <div style={{
        padding: theme.spacing.lg,
        borderBottom: `1px solid ${theme.colors.border}`,
        backgroundColor: theme.colors.backgroundAlt,
        flexShrink: 0
      }}>
        <h3 style={{
          margin: 0,
          fontSize: theme.fontSize.lg,
          fontWeight: theme.fontWeight.semibold,
          color: theme.colors.text,
          display: 'flex',
          alignItems: 'center',
          gap: theme.spacing.sm
        }}>
          {title}
        </h3>
        <p style={{
          margin: `${theme.spacing.xs} 0 0 0`,
          fontSize: theme.fontSize.sm,
          color: theme.colors.textSecondary
        }}>
          {description}
        </p>
      </div>

      {/* Maps Content */}
      <div style={{
        flex: 1,
        padding: theme.spacing.md,
        display: 'flex',
        flexDirection: 'column'
      }}>
        <GoogleMapsPicker
          lat={lat}
          lng={lng}
          onLocationClick={onLocationClick}
          apiKey={apiKey}
          zoom={zoom}
          mapTypeId={mapTypeId}
          height="100%"
          width="100%"
        />
      </div>

      {/* Coordinates Display */}
      <div style={{
        padding: theme.spacing.md,
        borderTop: `1px solid ${theme.colors.border}`,
        backgroundColor: theme.colors.backgroundAlt,
        fontSize: theme.fontSize.xs,
        color: theme.colors.textSecondary,
        fontFamily: 'var(--font-mono)',
        flexShrink: 0
      }}>
        <div style={{ display: 'flex', justifyContent: 'space-between' }}>
          <span>Lat: {lat.toFixed(6)}</span>
          <span>Lng: {lng.toFixed(6)}</span>
        </div>
        <div style={{
          marginTop: theme.spacing.xs,
          textAlign: 'center',
          fontSize: theme.fontSize.xs,
          opacity: 0.7
        }}>
          Click or drag marker to update coordinates
        </div>
      </div>
    </div>
  );
};

export default MapsPreviewPanel;