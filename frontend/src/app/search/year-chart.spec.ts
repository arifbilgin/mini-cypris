import { TestBed } from '@angular/core/testing';
import { YearChart, niceTicks } from './year-chart';

describe('YearChart', () => {
  function render(years: { year: number; count: number }[]) {
    const fixture = TestBed.createComponent(YearChart);
    fixture.componentRef.setInput('years', years);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('draws every year in the range, zero-filled', () => {
    const el = render([
      { year: 2019, count: 2 },
      { year: 2022, count: 8 },
    ]);
    expect(el.querySelectorAll('.bars path').length).toBe(4);
    expect(el.querySelectorAll('tbody tr').length).toBe(2); // the table lists real counts only
  });

  it('labels only the peak and describes itself', () => {
    const el = render([
      { year: 2019, count: 2 },
      { year: 2022, count: 1234 },
    ]);
    expect(el.querySelector('.peak')!.textContent!.trim()).toBe('1,234');
    expect(el.querySelector('svg')!.getAttribute('aria-label')).toBe(
      'Papers per year from 2019 to 2022. Most papers in 2022: 1234.',
    );
  });

  it('shows a tooltip on hover', () => {
    const fixture = TestBed.createComponent(YearChart);
    fixture.componentRef.setInput('years', [
      { year: 2020, count: 1 },
      { year: 2021, count: 7 },
    ]);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelector('.tooltip')).toBeNull();
    el.querySelectorAll('.hits rect')[1].dispatchEvent(new Event('mouseenter'));
    fixture.detectChanges();
    expect(el.querySelector('.tooltip')!.textContent!.replace(/\s+/g, ' ').trim()).toBe('2021 7 papers');
    el.querySelector('.frame')!.dispatchEvent(new Event('mouseleave'));
    fixture.detectChanges();
    expect(el.querySelector('.tooltip')).toBeNull();
  });

  it('picks round y ticks', () => {
    expect(niceTicks(638)).toEqual([0, 200, 400, 600, 800]);
    expect(niceTicks(3)).toEqual([0, 1, 2, 3]);
    expect(niceTicks(0)).toEqual([0, 1]);
    expect(niceTicks(4691)).toEqual([0, 2000, 4000, 6000]);
  });
});
