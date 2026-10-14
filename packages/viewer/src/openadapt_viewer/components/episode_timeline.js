/**
 * EpisodeTimeline - Reusable episode timeline component
 *
 * Usage:
 *   const timeline = new EpisodeTimeline({
 *     container: document.getElementById('timeline-container'),
 *     episodes: [...],
 *     currentTime: 0,
 *     totalDuration: 6.7,
 *     onSeek: (time) => { player.seek(time); },
 *     onEpisodeChange: (episode) => { console.log('Now in:', episode.name); }
 *   });
 *
 *   // Update current time (e.g., from playback loop)
 *   timeline.update({ currentTime: 4.2 });
 */

class EpisodeTimeline {
  constructor(options) {
    this.container = options.container;
    this.episodes = options.episodes || [];
    this.currentTime = options.currentTime || 0;
    this.totalDuration = options.totalDuration || this.calculateTotalDuration();
    this.onSeek = options.onSeek || (() => {});
    this.onEpisodeChange = options.onEpisodeChange || (() => {});

    this.config = {
      showLabels: true,
      showBoundaries: true,
      enableClickNavigation: true,
      enableAutoAdvance: false,
      colorScheme: 'auto',
      labelTruncate: 30,
      ...options.config
    };

    this.state = {
      currentEpisodeIndex: -1,
      hoveredEpisodeId: null,
      isDragging: false,
      previewTime: null
    };

    this.init();
  }

  init() {
    if (!this.container) {
      console.error('EpisodeTimeline: Container element not found');
      return;
    }

    if (!this.episodes.length) {
      console.warn('EpisodeTimeline: No episodes provided');
      this.renderEmpty();
      return;
    }

    this.updateCurrentEpisode();  // Update episode index before rendering
    this.render();
    this.attachEventListeners();
  }

  calculateTotalDuration() {
    if (!this.episodes.length) return 0;
    const lastEpisode = this.episodes[this.episodes.length - 1];
    return lastEpisode.end_time;
  }

  render() {
    const html = `
      <div class="oa-episode-timeline">
        ${this.renderCurrentIndicator()}
        ${this.renderLabels()}
        ${this.renderTrack()}
        ${this.renderControls()}
        ${this.renderTooltip()}
      </div>
    `;

    this.container.innerHTML = html;
  }

  renderCurrentIndicator() {
    if (this.state.currentEpisodeIndex < 0) return '';

    const episode = this.episodes[this.state.currentEpisodeIndex];
    const index = this.state.currentEpisodeIndex;

    return `
      <div class="oa-episode-current-indicator">
        <span class="oa-episode-current-label">Episode</span>
        <strong>${index + 1}</strong>
        <span>of</span>
        <strong>${this.episodes.length}</strong>
        <span class="oa-episode-divider">—</span>
        <span class="oa-episode-current-name">${episode.name}</span>
      </div>
    `;
  }

  renderLabels() {
    if (!this.config.showLabels) return '';

    const labels = this.episodes.map((episode, index) => {
      const left = (episode.start_time / this.totalDuration) * 100;
      const width = ((episode.end_time - episode.start_time) / this.totalDuration) * 100;
      const color = this.getEpisodeColor(index);
      const isCurrent = index === this.state.currentEpisodeIndex;
      const isPast = index < this.state.currentEpisodeIndex;
      const isFuture = index > this.state.currentEpisodeIndex;

      const classes = [
        'oa-episode-label',
        isCurrent && 'oa-episode-current',
        isPast && 'oa-episode-past',
        isFuture && 'oa-episode-future'
      ].filter(Boolean).join(' ');

      const truncatedName = this.truncateText(episode.name, this.config.labelTruncate);

      return `
        <div class="${classes}"
             data-episode-id="${episode.episode_id}"
             data-episode-index="${index}"
             style="left: ${left}%; width: ${width}%; background: ${color};"
             role="button"
             tabindex="0"
             aria-label="Jump to episode ${index + 1}: ${episode.name}">
          <span class="oa-episode-label-text">${truncatedName}</span>
          <span class="oa-episode-label-duration">${this.formatDuration(episode.duration)}</span>
        </div>
      `;
    }).join('');

    return `
      <div class="oa-episode-labels" role="group" aria-label="Episode labels">
        ${labels}
      </div>
    `;
  }

  renderTrack() {
    const segments = this.episodes.map((episode, index) => {
      const left = (episode.start_time / this.totalDuration) * 100;
      const width = ((episode.end_time - episode.start_time) / this.totalDuration) * 100;
      const color = this.getEpisodeColor(index);
      const isCurrent = index === this.state.currentEpisodeIndex;

      return `
        <div class="oa-episode-segment ${isCurrent ? 'oa-episode-current' : ''}"
             data-episode-index="${index}"
             style="left: ${left}%; width: ${width}%; background: ${color};">
        </div>
      `;
    }).join('');

    const boundaries = this.config.showBoundaries ?
      this.episodes.slice(0, -1).map((episode) => {
        const left = (episode.end_time / this.totalDuration) * 100;
        return `
          <div class="oa-episode-boundary"
               style="left: ${left}%;"
               role="separator">
          </div>
        `;
      }).join('') : '';

    const markerLeft = (this.currentTime / this.totalDuration) * 100;
    const currentMarker = `
      <div class="oa-current-marker"
           style="left: ${markerLeft}%;"
           role="slider"
           aria-label="Current playback position"
           aria-valuenow="${this.currentTime.toFixed(1)}"
           aria-valuemin="0"
           aria-valuemax="${this.totalDuration.toFixed(1)}">
      </div>
    `;

    return `
      <div class="oa-timeline-track"
           role="slider"
           aria-label="Playback timeline"
           tabindex="0">
        ${segments}
        ${boundaries}
        ${currentMarker}
      </div>
    `;
  }

  renderControls() {
    const hasPrev = this.state.currentEpisodeIndex > 0;
    const hasNext = this.state.currentEpisodeIndex < this.episodes.length - 1;

    return `
      <div class="oa-episode-controls">
        <button class="oa-episode-nav-btn"
                data-action="prev"
                ${!hasPrev ? 'disabled' : ''}
                aria-label="Go to previous episode">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
            <path d="M15.41 7.41L14 6l-6 6 6 6 1.41-1.41L10.83 12z"/>
          </svg>
          <span>Previous Episode</span>
        </button>

        <button class="oa-episode-nav-btn"
                data-action="next"
                ${!hasNext ? 'disabled' : ''}
                aria-label="Go to next episode">
          <span>Next Episode</span>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
            <path d="M10 6L8.59 7.41 13.17 12l-4.58 4.59L10 18l6-6z"/>
          </svg>
        </button>
      </div>
    `;
  }

  renderTooltip() {
    return `
      <div class="oa-episode-tooltip"
           role="tooltip"
           style="display: none;">
        <div class="oa-episode-tooltip-header">
          <strong class="oa-episode-tooltip-name"></strong>
          <span class="oa-episode-tooltip-meta"></span>
        </div>
        <div class="oa-episode-tooltip-description"></div>
        <div class="oa-episode-tooltip-footer"></div>
      </div>
    `;
  }

  renderEmpty() {
    this.container.innerHTML = `
      <div class="oa-episode-timeline-empty">
        <p>No episodes available for this recording.</p>
      </div>
    `;
  }

  attachEventListeners() {
    // Episode label clicks
    this.container.querySelectorAll('.oa-episode-label').forEach(label => {
      label.addEventListener('click', (e) => this.handleLabelClick(e));
      label.addEventListener('mouseenter', (e) => this.handleLabelHover(e));
      label.addEventListener('mouseleave', () => this.hideTooltip());
      label.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          this.handleLabelClick(e);
        }
      });
    });

    // Timeline track clicks
    const track = this.container.querySelector('.oa-timeline-track');
    if (track) {
      track.addEventListener('click', (e) => this.handleTrackClick(e));
      track.addEventListener('mousemove', (e) => this.handleTrackHover(e));
      track.addEventListener('mouseleave', () => this.hidePreview());
    }

    // Navigation buttons
    this.container.querySelectorAll('.oa-episode-nav-btn').forEach(btn => {
      btn.addEventListener('click', (e) => this.handleNavClick(e));
    });

    // Keyboard shortcuts
    this.handleKeydown = this.handleKeydown.bind(this);
    document.addEventListener('keydown', this.handleKeydown);
  }

  handleLabelClick(e) {
    if (!this.config.enableClickNavigation) return;

    const episodeId = e.currentTarget.dataset.episodeId;
    const episode = this.episodes.find(ep => ep.episode_id === episodeId);

    if (episode) {
      this.seekToEpisode(episode);
    }
  }

  handleLabelHover(e) {
    const episodeId = e.currentTarget.dataset.episodeId;
    const episode = this.episodes.find(ep => ep.episode_id === episodeId);

    if (episode) {
      this.showTooltip(episode, e);
    }
  }

  handleTrackClick(e) {
    const rect = e.currentTarget.getBoundingClientRect();
    const clickX = e.clientX - rect.left;
    const percent = clickX / rect.width;
    const time = percent * this.totalDuration;

    this.onSeek(time);
  }

  handleTrackHover(e) {
    const rect = e.currentTarget.getBoundingClientRect();
    const hoverX = e.clientX - rect.left;
    const percent = hoverX / rect.width;
    const time = percent * this.totalDuration;

    this.state.previewTime = time;
    // Could show preview marker here
  }

  handleNavClick(e) {
    const action = e.currentTarget.dataset.action;

    if (action === 'prev') {
      this.prevEpisode();
    } else if (action === 'next') {
      this.nextEpisode();
    }
  }

  handleKeydown(e) {
    // Only handle if timeline has focus or no other input is focused
    if (document.activeElement.tagName === 'INPUT') return;

    switch(e.key) {
      case 'ArrowLeft':
        e.preventDefault();
        this.prevEpisode();
        break;
      case 'ArrowRight':
        e.preventDefault();
        this.nextEpisode();
        break;
      case 'Home':
        e.preventDefault();
        this.seekToEpisode(this.episodes[0]);
        break;
      case 'End':
        e.preventDefault();
        this.seekToEpisode(this.episodes[this.episodes.length - 1]);
        break;
      default:
        // Number keys 1-9 for direct episode access
        if (e.key >= '1' && e.key <= '9') {
          const index = parseInt(e.key) - 1;
          if (index < this.episodes.length) {
            e.preventDefault();
            this.seekToEpisode(this.episodes[index]);
          }
        }
    }
  }

  showTooltip(episode, event) {
    const tooltip = this.container.querySelector('.oa-episode-tooltip');
    if (!tooltip) return;

    const nameEl = tooltip.querySelector('.oa-episode-tooltip-name');
    const metaEl = tooltip.querySelector('.oa-episode-tooltip-meta');
    const descEl = tooltip.querySelector('.oa-episode-tooltip-description');
    const footerEl = tooltip.querySelector('.oa-episode-tooltip-footer');

    nameEl.textContent = episode.name;
    metaEl.textContent = `${this.formatDuration(episode.duration)} • ${episode.steps?.length || 0} steps`;
    descEl.textContent = episode.description;

    if (episode.boundary_confidence) {
      footerEl.textContent = `Confidence: ${(episode.boundary_confidence * 100).toFixed(0)}%`;
    }

    // Position tooltip
    const labelRect = event.currentTarget.getBoundingClientRect();
    const containerRect = this.container.getBoundingClientRect();

    tooltip.style.left = `${labelRect.left - containerRect.left}px`;
    tooltip.style.top = `${labelRect.top - containerRect.top - tooltip.offsetHeight - 8}px`;
    tooltip.style.display = 'block';

    this.state.hoveredEpisodeId = episode.episode_id;
  }

  hideTooltip() {
    const tooltip = this.container.querySelector('.oa-episode-tooltip');
    if (tooltip) {
      tooltip.style.display = 'none';
    }
    this.state.hoveredEpisodeId = null;
  }

  hidePreview() {
    this.state.previewTime = null;
  }

  seekToEpisode(episode) {
    this.onSeek(episode.start_time);
  }

  prevEpisode() {
    if (this.state.currentEpisodeIndex > 0) {
      const prevEp = this.episodes[this.state.currentEpisodeIndex - 1];
      this.seekToEpisode(prevEp);
    }
  }

  nextEpisode() {
    if (this.state.currentEpisodeIndex < this.episodes.length - 1) {
      const nextEp = this.episodes[this.state.currentEpisodeIndex + 1];
      this.seekToEpisode(nextEp);
    }
  }

  update(updates) {
    let needsRender = false;

    if (updates.currentTime !== undefined && updates.currentTime !== this.currentTime) {
      this.currentTime = updates.currentTime;
      this.updateCurrentEpisode();
      needsRender = true;
    }

    if (updates.episodes !== undefined) {
      this.episodes = updates.episodes;
      needsRender = true;
    }

    if (needsRender) {
      this.render();
      this.attachEventListeners();
    } else {
      // Just update marker position (more efficient)
      this.updateMarkerPosition();
    }
  }

  updateCurrentEpisode() {
    const previousIndex = this.state.currentEpisodeIndex;

    // Find which episode we're in
    for (let i = 0; i < this.episodes.length; i++) {
      const ep = this.episodes[i];
      if (this.currentTime >= ep.start_time && this.currentTime < ep.end_time) {
        this.state.currentEpisodeIndex = i;
        break;
      }
    }

    // If we've crossed a boundary, fire callback
    if (previousIndex !== this.state.currentEpisodeIndex &&
        this.state.currentEpisodeIndex >= 0) {
      const episode = this.episodes[this.state.currentEpisodeIndex];
      this.onEpisodeChange(episode);
    }
  }

  updateMarkerPosition() {
    const marker = this.container.querySelector('.oa-current-marker');
    if (marker) {
      const left = (this.currentTime / this.totalDuration) * 100;
      marker.style.left = `${left}%`;
      marker.setAttribute('aria-valuenow', this.currentTime.toFixed(1));
    }
  }

  getEpisodeColor(index) {
    const colorIndex = (index % 5) + 1;
    return `var(--episode-${colorIndex}-bg)`;
  }

  formatDuration(seconds) {
    if (seconds < 60) {
      return `${seconds.toFixed(1)}s`;
    }
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return `${mins}m ${secs}s`;
  }

  truncateText(text, maxLength) {
    if (text.length <= maxLength) return text;
    return text.substring(0, maxLength - 3) + '...';
  }

  destroy() {
    // Clean up event listeners
    document.removeEventListener('keydown', this.handleKeydown);
    this.container.innerHTML = '';
  }
}

// Export for use in modules
if (typeof module !== 'undefined' && module.exports) {
  module.exports = EpisodeTimeline;
}
