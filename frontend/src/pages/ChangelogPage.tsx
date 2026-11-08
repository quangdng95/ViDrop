import { type Page } from "../components/Header";
import SectionCard from "../components/SectionCard";
import { useLanguage } from "../i18n/LanguageContext";

interface ChangelogPageProps {
  onNavigate: (page: Page) => void;
}

const ChangelogPage = ({ onNavigate: _onNavigate }: ChangelogPageProps) => {
  const { t } = useLanguage();

  return (
    <div className="w-full flex flex-col gap-5 sm:gap-6">
      {t.changelog.releases.map((release) => (
        <SectionCard
          key={`${release.date}|${release.title}`}
          className="p-0 bg-white border border-ink  rounded-none overflow-hidden flex flex-col gap-0"
        >
          {/* Stacked on narrow screens so a long title gets the full card width to
              wrap into, instead of crowding to the right of a fixed-width date
              column and wrapping with a ragged left edge. */}
          <div className="flex flex-col sm:flex-row sm:items-baseline gap-1 sm:gap-3 px-4 py-3.5 sm:px-6 sm:py-4 bg-muted border-b border-ink">
            <span className="font-mono text-sm sm:text-base font-bold text-brand shrink-0 whitespace-nowrap">
              {release.date}
            </span>
            <span className="text-base sm:text-lg font-extrabold leading-snug text-ink">{release.title}</span>
          </div>
          <ul className="flex flex-col px-4 sm:px-6 list-none pl-0">
            {release.items.map((item, i) => (
              <li
                key={i}
                className="text-base leading-relaxed py-3.5 sm:py-4 border-t border-dashed border-ink first:border-t-0 text-ink"
              >
                {item}
              </li>
            ))}
          </ul>
        </SectionCard>
      ))}
    </div>
  );
};

export default ChangelogPage;
