import { Routes } from '@angular/router';
import { SearchPage } from './search/search-page';

export const routes: Routes = [
  { path: '', component: SearchPage, title: 'Mini-Cypris: search research papers' },
  { path: '**', redirectTo: '' },
];
