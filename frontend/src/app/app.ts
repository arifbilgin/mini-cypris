import { Component, VERSION, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';

interface Hello {
  message: string;
  name: string;
  served_at: number;
}

interface Health {
  status: string;
  version: string;
  db: string;
  tables: string[];
}

@Component({
  selector: 'app-root',
  templateUrl: './app.html',
  styleUrl: './app.scss',
})
export class App {
  private readonly http = inject(HttpClient);

  protected readonly angularVersion = VERSION.full;
  protected readonly hello = signal<Hello | null>(null);
  protected readonly health = signal<Health | null>(null);
  protected readonly error = signal<string | null>(null);

  constructor() {
    this.http.get<Hello>('/api/hello').subscribe({
      next: (h) => this.hello.set(h),
      error: () => this.error.set('Could not reach the API. Is Flask running on port 8000?'),
    });
    this.http.get<Health>('/api/health').subscribe({
      next: (h) => this.health.set(h),
      error: () => this.health.set(null),
    });
  }
}
