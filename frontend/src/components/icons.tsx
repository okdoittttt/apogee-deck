type IconProps = { className?: string };

const base = {
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.7,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
};

export const LogoIcon = ({ className = "h-6 w-6" }: IconProps) => (
  <svg viewBox="0 0 24 24" className={className} aria-hidden>
    <rect x="2" y="2" width="20" height="20" rx="6" fill="currentColor" opacity="0.12" />
    <circle cx="12" cy="7.5" r="2" fill="currentColor" />
    <circle cx="7" cy="16" r="2" fill="currentColor" />
    <circle cx="17" cy="16" r="2" fill="currentColor" />
    <path d="M12 9.5 7.8 14.3M12 9.5l4.2 4.8M9 16h6" stroke="currentColor" strokeWidth="1.4" />
  </svg>
);

export const SidebarIcon = ({ className = "h-5 w-5" }: IconProps) => (
  <svg viewBox="0 0 24 24" className={className} {...base} aria-hidden>
    <rect x="3" y="4" width="18" height="16" rx="3" />
    <path d="M9 4v16" />
  </svg>
);

export const PlusIcon = ({ className = "h-5 w-5" }: IconProps) => (
  <svg viewBox="0 0 24 24" className={className} {...base} aria-hidden>
    <path d="M12 5v14M5 12h14" />
  </svg>
);

export const SearchIcon = ({ className = "h-5 w-5" }: IconProps) => (
  <svg viewBox="0 0 24 24" className={className} {...base} aria-hidden>
    <circle cx="11" cy="11" r="6.5" />
    <path d="m20 20-4.2-4.2" />
  </svg>
);

export const TrashIcon = ({ className = "h-4 w-4" }: IconProps) => (
  <svg viewBox="0 0 24 24" className={className} {...base} aria-hidden>
    <path d="M4.5 7h15M10 11v6M14 11v6M6.5 7l.8 11.2A2 2 0 0 0 9.3 20h5.4a2 2 0 0 0 2-1.8L17.5 7M9 7V5.5A1.5 1.5 0 0 1 10.5 4h3A1.5 1.5 0 0 1 15 5.5V7" />
  </svg>
);

export const ArrowUpIcon = ({ className = "h-4 w-4" }: IconProps) => (
  <svg viewBox="0 0 24 24" className={className} {...base} strokeWidth={2.2} aria-hidden>
    <path d="M12 19V5M6 11l6-6 6 6" />
  </svg>
);

export const StopIcon = ({ className = "h-3.5 w-3.5" }: IconProps) => (
  <svg viewBox="0 0 24 24" className={className} aria-hidden>
    <rect x="5" y="5" width="14" height="14" rx="2.5" fill="currentColor" />
  </svg>
);

export const CloseIcon = ({ className = "h-3 w-3" }: IconProps) => (
  <svg viewBox="0 0 24 24" className={className} {...base} strokeWidth={2.4} aria-hidden>
    <path d="M6 6l12 12M18 6 6 18" />
  </svg>
);

export const ImageIcon =({ className = "h-4 w-4" }: IconProps) => (
  <svg viewBox="0 0 24 24" className={className} {...base} aria-hidden>
    <rect x="3" y="4" width="18" height="16" rx="3" />
    <circle cx="9" cy="10" r="1.6" />
    <path d="m21 16-5-5-8.5 9" />
  </svg>
);
