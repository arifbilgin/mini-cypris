import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { App } from './app';

describe('App', () => {
  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [App],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
  });

  it('renders the hello message from the API', async () => {
    const fixture = TestBed.createComponent(App);
    const http = TestBed.inject(HttpTestingController);

    http.expectOne('/api/hello').flush({ message: 'Hello, world! This is Flask.', name: 'world', served_at: 0 });
    http.expectOne('/api/health').flush({ status: 'ok', version: '0.1.0', db: 'ok', tables: ['papers'] });
    await fixture.whenStable();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('Hello, Mini-Cypris');
    expect(text).toContain('Hello, world! This is Flask.');
    http.verify();
  });

  it('shows an error when the API is down', async () => {
    const fixture = TestBed.createComponent(App);
    const http = TestBed.inject(HttpTestingController);

    http.expectOne('/api/hello').flush('down', { status: 502, statusText: 'Bad Gateway' });
    http.expectOne('/api/health').flush('down', { status: 502, statusText: 'Bad Gateway' });
    await fixture.whenStable();

    expect((fixture.nativeElement as HTMLElement).textContent).toContain('Could not reach the API');
  });
});
